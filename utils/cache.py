"""Shared, concurrency-safe pools for public ports and per-instance subnets.

Redis is required for production multi-worker deployments. The filesystem/simple
backend remains suitable only for local testing and is not a distributed lock.
"""
import ipaddress
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

    def init_port_sets(self):
        """Reconcile free resources without making allocated resources available."""
        min_port = int(get_config("whale:frp_direct_port_minimum", 10000))
        max_port = int(get_config("whale:frp_direct_port_maximum", 10100))
        if not (1 <= min_port <= max_port <= 65535):
            raise ValueError(
                "Invalid FRP direct port range; expected 1 <= minimum <= maximum <= 65535."
            )

        # Read persisted allocations under the same Redis lock as allocators. This
        # avoids a stale DB snapshot reintroducing a port just allocated elsewhere.
        self.provider.reconcile_port_pool(
            set(range(min_port, max_port + 1)),
            self._used_public_ports_from_db,
        )

        # A Docker API outage must not prevent the direct port pool from being
        # reconciled. Subnets are reconciled only when Swarm can be queried.
        try:
            from .docker import get_docker_client

            client = get_docker_client()
            base_network = ipaddress.ip_network(
                get_config("whale:docker_subnet", "174.1.0.0/16"),
                strict=False,
            )
            new_prefix = int(get_config("whale:docker_subnet_new_prefix", "24"))
            if not (base_network.prefixlen <= new_prefix <= base_network.max_prefixlen):
                raise ValueError(
                    "Docker subnet prefix must be between the configured subnet prefix "
                    "and the address-family maximum."
                )
            network_candidates = {
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

            # The provider invokes this callback while holding the pool lock, so
            # the network discovery snapshot cannot race a new subnet allocation.
            self.provider.reconcile_network_pool(
                network_candidates, existing_network_prefixes
            )
        except Exception as exc:
            warnings.warn(
                f"[CTFd-Whale] Unable to reconcile Docker network subnets: {exc}",
                RuntimeWarning,
            )

    @staticmethod
    def _used_public_ports_from_db():
        rows = (
            db.session.query(WhaleContainerPort.public_port)
            .filter(WhaleContainerPort.public_port.isnot(None))
            .all()
        )
        return {int(row[0]) for row in rows}

    def add_available_port(self, port):
        """Return a port only when no persisted instance currently owns it."""
        port = int(port)
        if (
            db.session.query(WhaleContainerPort.id)
            .filter(WhaleContainerPort.public_port == port)
            .first()
            is not None
        ):
            return False
        return self.provider.add_available_port(port)

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

    def add_available_port(self, port):
        port = int(port)
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

    def add_available_network_range(self, *ranges):
        reserved = self._read_set(self.network_reservation_key)
        available = self._read_set(self.global_network_key)
        for subnet in ranges:
            reserved.discard(str(subnet))
            available.add(str(subnet))
        cache.set(self.network_reservation_key, reserved)
        cache.set(self.global_network_key, available)

    def acquire_lock(self):
        return True

    def release_lock(self):
        return True


class RedisCacheProvider(FlaskRedis):
    def __init__(self, app, *args, **kwargs):
        super().__init__(app)
        self.key = "ctfd_whale_lock-" + str(kwargs.get("user_id", 0))
        self.current_lock = None
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
            value = self.spop(self.global_port_key)
            if value is None:
                return None
            port = int(value)
            self.set(self._reservation_key("port", port), "1", ex=RESERVATION_TTL_SECONDS)
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

    def add_available_port(self, port):
        port = int(port)
        minimum = int(get_config("whale:frp_direct_port_minimum", 10000))
        maximum = int(get_config("whale:frp_direct_port_maximum", 10100))
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis port-pool lock.")
        try:
            self.delete(self._reservation_key("port", port))
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
            value = self.spop(self.global_network_key)
            if value is None:
                return None
            subnet = value.decode() if isinstance(value, bytes) else str(value)
            self.set(
                self._reservation_key("network", subnet),
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

    def add_available_network_range(self, *ranges):
        lock = self._pool_lock()
        if lock is None:
            raise RuntimeError("Unable to acquire the Redis network-pool lock.")
        try:
            for subnet in ranges:
                self.delete(self._reservation_key("network", str(subnet)))
            if ranges:
                self.sadd(self.global_network_key, *[str(item) for item in ranges])
        finally:
            lock.release()

    def clear(self):
        self.delete(self.global_port_key, self.global_network_key)
        # Per-resource reservation keys have TTLs and expire automatically.

    def acquire_lock(self):
        lock = self.lock(name=self.key, timeout=10)
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
