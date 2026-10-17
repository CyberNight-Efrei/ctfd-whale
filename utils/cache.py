"""Shared, concurrency-safe pools for public ports and per-instance subnets.

Redis is required for production multi-worker deployments. The filesystem/simple
backend remains suitable only for local testing and is not a distributed lock.
"""
import ipaddress
import threading
import warnings

from CTFd.cache import cache
from CTFd.models import db
from CTFd.utils import get_config
from flask_redis import FlaskRedis
from redis.exceptions import LockError

from ..models import WhaleContainerPort


PORT_POOL_KEY = "ctfd_whale-port-set"
NETWORK_POOL_KEY = "ctfd_whale-network-set"
PORT_RESERVATION_PREFIX = "ctfd_whale-port-reservation:"
NETWORK_RESERVATION_PREFIX = "ctfd_whale-network-reservation:"
POOL_LOCK_KEY = "ctfd_whale-pool-allocation-lock"
RESERVATION_TTL_SECONDS = 300


class CacheProvider:
    _lifecycle_locks = {}
    _lifecycle_locks_guard = threading.Lock()
    _capacity_lock = threading.Lock()

    def __init__(self, app, *args, **kwargs):
        cache_type = app.config.get("CACHE_TYPE", "simple")
        if cache_type == "redis":
            self.provider = RedisCacheProvider(app, *args, **kwargs)
        elif cache_type in ("filesystem", "simple"):
            if not hasattr(CacheProvider, "cache"):
                CacheProvider.cache = {}
            self.provider = FilesystemCacheProvider(app, *args, **kwargs)
        else:
            raise ValueError(
                f"Unsupported CTFd cache backend for CTFd-Whale: {cache_type}"
            )

    def _public_port_candidates(self):
        minimum = int(get_config("whale:frp_direct_port_minimum", 10000))
        maximum = int(get_config("whale:frp_direct_port_maximum", 10100))
        if not (1 <= minimum <= maximum <= 65535):
            raise ValueError(
                "Invalid FRP direct port range; expected 1 <= minimum <= maximum <= 65535."
            )
        return set(range(minimum, maximum + 1))

    def _reconcile_public_ports(self):
        # The Redis provider invokes this DB callback under its allocation lock.
        self.provider.reconcile_port_pool(
            self._public_port_candidates(),
            self._used_public_ports_from_db,
        )

    def _network_pool_snapshot(self):
        from .docker import get_docker_client

        client = get_docker_client()
        base_network = ipaddress.ip_network(
            get_config("whale:docker_subnet", "174.1.0.0/16"),
            strict=False,
        )
        new_prefix = int(get_config("whale:docker_subnet_new_prefix", "24"))
        if base_network.version != 4:
            raise ValueError("CTFd-Whale grouped challenge pools currently require an IPv4 subnet.")
        if not (base_network.prefixlen <= new_prefix <= 32):
            raise ValueError(
                "Docker subnet prefix must be between the configured subnet prefix and 32."
            )
        subnet_count = 1 << (new_prefix - base_network.prefixlen)
        if subnet_count > 65536:
            raise ValueError(
                f"Docker subnet range would create {subnet_count} candidate subnets; "
                "use a narrower range or larger subnet prefix."
            )
        candidates = {
            str(subnet) for subnet in base_network.subnets(new_prefix=new_prefix)
        }

        def existing_network_prefixes():
            prefixes = set()
            for network in client.networks.list(filters={"label": "prefix"}):
                attrs = getattr(network, "attrs", {}) or {}
                labels = attrs.get("Labels") or {}
                prefix = labels.get("prefix")
                if prefix:
                    prefixes.add(str(prefix))
            return prefixes

        return candidates, existing_network_prefixes

    def _reconcile_network_pool(self):
        candidates, existing_networks = self._network_pool_snapshot()
        # This callback is invoked while the provider's distributed allocation
        # lock is held so a concurrent subnet reservation cannot be lost.
        self.provider.reconcile_network_pool(candidates, existing_networks)

    def init_port_sets(self):
        """Reconcile free resources without making allocated resources available."""
        self._reconcile_public_ports()
        try:
            self._reconcile_network_pool()
        except Exception as exc:
            # Direct ports remain available even if Docker/Swarm is unreachable.
            warnings.warn(
                f"[CTFd-Whale] Unable to reconcile Docker network subnets: {exc}",
                RuntimeWarning,
            )

    def get_available_port(self):
        # Reconcile *before* popping: old/stale pool members must never be handed
        # out before persisted DB ownership has been checked under the Redis lock.
        try:
            self._reconcile_public_ports()
        except Exception as exc:
            warnings.warn(
                f"[CTFd-Whale] Unable to safely reconcile the public port pool: {exc}",
                RuntimeWarning,
            )
            return None

        port = self.provider.get_available_port()
        if port is not None:
            return port

        # A concurrent allocator may have taken the final free port after our
        # reconciliation. Refresh once more; active leases are excluded atomically.
        try:
            self._reconcile_public_ports()
        except Exception as exc:
            warnings.warn(
                f"[CTFd-Whale] Unable to refresh the public port pool: {exc}",
                RuntimeWarning,
            )
            return None
        return self.provider.get_available_port()

    def get_available_network_range(self):
        # Never hand out a subnet from a stale pool before checking Swarm ownership.
        # Reconciliation also recovers leases abandoned by workers that crashed.
        try:
            self._reconcile_network_pool()
        except Exception as exc:
            warnings.warn(
                f"[CTFd-Whale] Unable to safely reconcile Docker network subnets: {exc}",
                RuntimeWarning,
            )
            return None

        subnet = self.provider.get_available_network_range()
        if subnet is not None:
            return subnet

        # Another request may have taken the last subnet after reconciliation.
        try:
            self._reconcile_network_pool()
        except Exception as exc:
            warnings.warn(
                f"[CTFd-Whale] Unable to refresh Docker network subnets: {exc}",
                RuntimeWarning,
            )
            return None
        return self.provider.get_available_network_range()

    @staticmethod
    def _used_public_ports_from_db():
        rows = (
            db.session.query(WhaleContainerPort.public_port)
            .filter(WhaleContainerPort.public_port.isnot(None))
            .all()
        )
        return {int(row[0]) for row in rows}

    @staticmethod
    def _is_public_port_used(port):
        return (
            db.session.query(WhaleContainerPort.id)
            .filter(WhaleContainerPort.public_port == int(port))
            .first()
            is not None
        )

    def add_available_port(self, port):
        """Return a public port without overriding an in-flight reservation."""
        return self.provider.add_available_port(
            int(port),
            is_used=lambda: self._is_public_port_used(port),
        )

    def add_available_network_range(self, *ranges):
        """Return subnets only when neither a live network nor a lease owns them."""
        try:
            from .docker import get_docker_client

            client = get_docker_client()

            def used_subnets():
                used = set()
                for network in client.networks.list(filters={"label": "prefix"}):
                    attrs = getattr(network, "attrs", {}) or {}
                    labels = attrs.get("Labels") or {}
                    prefix = labels.get("prefix")
                    if prefix:
                        used.add(str(prefix))
                return used

            return self.provider.add_available_network_range(
                *ranges, is_used=used_subnets
            )
        except Exception as exc:
            # Fail closed. Pool reconciliation will re-discover the subnet later;
            # never mark it free when current Docker ownership is unknown.
            warnings.warn(
                f"[CTFd-Whale] Unable to safely return Docker subnet to the pool: {exc}",
                RuntimeWarning,
            )
            return False

    def acquire_lifecycle_lock(self, user_id):
        return self.provider.acquire_lifecycle_lock(user_id)

    def release_lifecycle_lock(self):
        return self.provider.release_lifecycle_lock()

    def acquire_capacity_lock(self):
        return self.provider.acquire_capacity_lock()

    def release_capacity_lock(self):
        return self.provider.release_capacity_lock()

    def __getattr__(self, name):
        return self.provider.__getattribute__(name)


class FilesystemCacheProvider:
    """Single-process development provider; not safe for production concurrency."""

    def __init__(self, app, *args, **kwargs):
        warnings.warn(
            "\n[CTFd Whale] Warning: filesystem/simple cache is for testing only. "
            "\nUse Redis for production and multiple CTFd workers.",
            RuntimeWarning,
        )
        self.key = "ctfd_whale_lock-" + str(kwargs.get("user_id", 0))
        self.global_port_key = PORT_POOL_KEY
        self.global_network_key = NETWORK_POOL_KEY
        self.port_reservation_key = PORT_POOL_KEY + "-reservations"
        self.network_reservation_key = NETWORK_POOL_KEY + "-reservations"
        self.current_lifecycle_lock = None
        self.current_capacity_lock = False

    @staticmethod
    def _read_set(key):
        return set(cache.get(key) or ())

    def clear(self):
        cache.set(self.global_port_key, set())
        cache.set(self.global_network_key, set())
        cache.set(self.port_reservation_key, set())
        cache.set(self.network_reservation_key, set())

    def reconcile_port_pool(self, candidates, used):
        if callable(used):
            used = used()
        reserved = {int(item) for item in self._read_set(self.port_reservation_key)}
        available = set(candidates) - {int(item) for item in used} - reserved
        cache.set(self.global_port_key, available)

    def reconcile_network_pool(self, candidates, used):
        if callable(used):
            used = used()
        reserved = self._read_set(self.network_reservation_key)
        cache.set(self.global_network_key, set(candidates) - set(used) - reserved)

    def get_available_port(self):
        available = {int(item) for item in self._read_set(self.global_port_key)}
        if not available:
            return None
        port = available.pop()
        cache.set(self.global_port_key, available)
        reserved = {int(item) for item in self._read_set(self.port_reservation_key)}
        reserved.add(port)
        cache.set(self.port_reservation_key, reserved)
        return port

    def commit_port_reservation(self, port):
        reserved = {int(item) for item in self._read_set(self.port_reservation_key)}
        reserved.discard(int(port))
        cache.set(self.port_reservation_key, reserved)

    def add_available_port(self, port, is_used=None):
        port = int(port)
        if is_used is not None and is_used():
            return False
        reserved = {int(item) for item in self._read_set(self.port_reservation_key)}
        reserved.discard(port)
        cache.set(self.port_reservation_key, reserved)
        minimum = int(get_config("whale:frp_direct_port_minimum", 10000))
        maximum = int(get_config("whale:frp_direct_port_maximum", 10100))
        if minimum <= port <= maximum:
            available = {int(item) for item in self._read_set(self.global_port_key)}
            available.add(port)
            cache.set(self.global_port_key, available)
        return True

    def get_available_network_range(self):
        available = self._read_set(self.global_network_key)
        if not available:
            return None
        subnet = available.pop()
        cache.set(self.global_network_key, available)
        reserved = self._read_set(self.network_reservation_key)
        reserved.add(subnet)
        cache.set(self.network_reservation_key, reserved)
        return str(subnet)

    def commit_network_reservation(self, subnet):
        reserved = self._read_set(self.network_reservation_key)
        reserved.discard(str(subnet))
        cache.set(self.network_reservation_key, reserved)

    def add_available_network_range(self, *ranges, is_used=None):
        used = set(is_used()) if is_used is not None else set()
        reserved = self._read_set(self.network_reservation_key)
        available = self._read_set(self.global_network_key)
        for subnet in ranges:
            subnet = str(subnet)
            if subnet in used or subnet in reserved:
                continue
            available.add(subnet)
        cache.set(self.global_network_key, available)

    def acquire_lifecycle_lock(self, user_id):
        # Protect multiple request threads inside this development process. This
        # is not distributed; Redis is still required for multi-worker production.
        with CacheProvider._lifecycle_locks_guard:
            lock = CacheProvider._lifecycle_locks.setdefault(
                str(user_id), threading.Lock()
            )
        if not lock.acquire(timeout=10):
            return False
        self.current_lifecycle_lock = lock
        return True

    def release_lifecycle_lock(self):
        lock = self.current_lifecycle_lock
        self.current_lifecycle_lock = None
        if lock is None:
            return False
        lock.release()
        return True

    def acquire_capacity_lock(self):
        if not CacheProvider._capacity_lock.acquire(timeout=10):
            return False
        self.current_capacity_lock = True
        return True

    def release_capacity_lock(self):
        if not self.current_capacity_lock:
            return False
        self.current_capacity_lock = False
        CacheProvider._capacity_lock.release()
        return True

    def acquire_lock(self):
        return True

    def release_lock(self):
        return True


class RedisCacheProvider(FlaskRedis):
    def __init__(self, app, *args, **kwargs):
        super().__init__(app)
        self.key = "ctfd_whale_lock-" + str(kwargs.get("user_id", 0))
        self.current_lock = None
        self.current_lifecycle_lock = None
        self.current_capacity_lock = None
        self.global_port_key = PORT_POOL_KEY
        self.global_network_key = NETWORK_POOL_KEY

    def _pool_lock(self):
        lock = self.lock(name=POOL_LOCK_KEY, timeout=120)
        if not lock.acquire(blocking=True, blocking_timeout=10):
            return None
        return lock

    def _reservation_key(self, resource, value):
        prefix = PORT_RESERVATION_PREFIX if resource == "port" else NETWORK_RESERVATION_PREFIX
        return prefix + str(value)

    def _active_reservations(self, resource, candidates):
        candidates = list(candidates)
        if not candidates:
            return set()
        pipeline = self.pipeline()
        for candidate in candidates:
            pipeline.exists(self._reservation_key(resource, candidate))
        results = pipeline.execute()
        return {
            candidate for candidate, exists in zip(candidates, results) if exists
        }

    def reconcile_port_pool(self, candidates, used):
        candidates = {int(item) for item in candidates}
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis port-pool lock.")
        try:
            if callable(used):
                used = used()
            used = {int(item) for item in used}
            reserved = self._active_reservations("port", candidates)
            available = candidates - used - reserved
            self.delete(self.global_port_key)
            if available:
                self.sadd(self.global_port_key, *sorted(available))
        finally:
            lock.release()

    def reconcile_network_pool(self, candidates, used):
        candidates = {str(item) for item in candidates}
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis network-pool lock.")
        try:
            if callable(used):
                used = used()
            used = {str(item) for item in used}
            reserved = self._active_reservations("network", candidates)
            available = candidates - used - reserved
            self.delete(self.global_network_key)
            if available:
                self.sadd(self.global_network_key, *sorted(available))
        finally:
            lock.release()

    def get_available_port(self):
        lock = self._pool_lock()
        if lock is None:
            return None
        try:
            while True:
                value = self.spop(self.global_port_key)
                if value is None:
                    return None
                port = int(value)
                reservation_key = self._reservation_key("port", port)
                if self.exists(reservation_key):
                    # Discard stale pool entries that conflict with a live lease.
                    continue
                self.set(reservation_key, "1", ex=RESERVATION_TTL_SECONDS)
                return port
        finally:
            lock.release()

    def commit_port_reservation(self, port):
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis port-pool lock.")
        try:
            self.delete(self._reservation_key("port", int(port)))
        finally:
            lock.release()

    def add_available_port(self, port, is_used=None):
        port = int(port)
        minimum = int(get_config("whale:frp_direct_port_minimum", 10000))
        maximum = int(get_config("whale:frp_direct_port_maximum", 10100))
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis port-pool lock.")
        try:
            # A concurrent allocator may have reserved this port after its former
            # DB mapping was cleared. Never delete its lease or reinsert the port.
            if self.exists(self._reservation_key("port", port)):
                return False
            if is_used is not None and is_used():
                return False
            if minimum <= port <= maximum:
                self.sadd(self.global_port_key, str(port))
        finally:
            lock.release()
        return True

    def get_available_network_range(self):
        lock = self._pool_lock()
        if lock is None:
            return None
        try:
            while True:
                value = self.spop(self.global_network_key)
                if value is None:
                    return None
                subnet = value.decode() if isinstance(value, bytes) else str(value)
                reservation_key = self._reservation_key("network", subnet)
                if self.exists(reservation_key):
                    # Discard stale pool entries that conflict with a live lease.
                    continue
                self.set(
                    reservation_key,
                    "1",
                    ex=RESERVATION_TTL_SECONDS,
                )
                return subnet
        finally:
            lock.release()

    def commit_network_reservation(self, subnet):
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis network-pool lock.")
        try:
            self.delete(self._reservation_key("network", str(subnet)))
        finally:
            lock.release()

    def add_available_network_range(self, *ranges, is_used=None):
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis network-pool lock.")
        try:
            currently_used = set(is_used()) if is_used is not None else set()
            available = []
            for subnet in ranges:
                subnet = str(subnet)
                # A concurrent grouped challenge may have reserved this subnet
                # after the old network was removed. Preserve its lease and also
                # verify that Docker has not already attached the subnet elsewhere.
                if self.exists(self._reservation_key("network", subnet)):
                    continue
                if subnet in currently_used:
                    continue
                available.append(subnet)
            if available:
                self.sadd(self.global_network_key, *available)
        finally:
            lock.release()

    def clear(self):
        self.delete(self.global_port_key, self.global_network_key)
        # Per-resource reservation keys have TTLs and expire automatically.

    def acquire_lifecycle_lock(self, user_id):
        # All create/delete paths (including admin actions and the janitor) use
        # one per-user lifecycle lock, independent of the rate-limit lock.
        timeout = max(600, int(get_config("whale:docker_timeout", "3600")))
        lock = self.lock(name=f"ctfd_whale_lifecycle-{user_id}", timeout=timeout)
        if not lock.acquire(blocking=True, blocking_timeout=10.0):
            return False
        self.current_lifecycle_lock = lock
        return True

    def release_lifecycle_lock(self):
        lock = self.current_lifecycle_lock
        self.current_lifecycle_lock = None
        if lock is None:
            return False
        try:
            lock.release()
            return True
        except LockError:
            return False

    def acquire_capacity_lock(self):
        lock = self.lock(name="ctfd_whale_capacity", timeout=30)
        if not lock.acquire(blocking=True, blocking_timeout=10.0):
            return False
        self.current_capacity_lock = lock
        return True

    def release_capacity_lock(self):
        lock = self.current_capacity_lock
        self.current_capacity_lock = None
        if lock is None:
            return False
        try:
            lock.release()
            return True
        except LockError:
            return False

    def acquire_lock(self):
        # The rate-limit lock is independent from the per-user lifecycle lock.
        timeout = max(600, int(get_config("whale:docker_timeout", "3600")))
        lock = self.lock(name=self.key, timeout=timeout)
        if not lock.acquire(blocking=True, blocking_timeout=2.0):
            return False
        self.current_lock = lock
        return True

    def release_lock(self):
        if self.current_lock is None:
            return False
        try:
            self.current_lock.release()
            return True
        except LockError:
            return False
