import html
import re
import warnings

from flask import current_app
from requests import session, RequestException

from CTFd.models import db
from CTFd.utils import get_config, set_config, logging

from .base import BaseRouter
from ..cache import CacheProvider
from ..db import DBContainer
from ..exceptions import WhaleError, WhaleWarning
from ...models import WhaleContainer


class FrpRouter(BaseRouter):
    name = "frp"
    types = {"tcp": "tcp", "ssh": "tcp", "http": "http"}

    class FrpRule:
        def __init__(self, name, config):
            self.name = name
            self.config = config

        def __str__(self):
            return f'[{self.name}]\n' + "\n".join(
                f"{key} = {value}" for key, value in self.config.items()
            )

    def __init__(self):
        super().__init__()
        self.ses = session()
        self.url = get_config("whale:frp_api_url", "").rstrip("/")
        self.common = ""
        try:
            CacheProvider(app=current_app).init_port_sets()
        except Exception:
            warnings.warn("cache initialization failed", WhaleWarning)

    @staticmethod
    def _safe_rule_name(value):
        return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))

    @staticmethod
    def _http_subdomain(container, port_mapping, http_count):
        base = container.http_subdomain
        if http_count > 1:
            return f"{base}-{port_mapping.name}"
        return base

    def _rules_for_container(self, container):
        mappings = list(container.port_mappings)
        http_count = sum(1 for mapping in mappings if mapping.protocol == "http")
        rules = []
        for mapping in mappings:
            rule_name = self._safe_rule_name(
                f"{mapping.protocol}_{container.user_id}_{container.uuid}_{mapping.name}"
            )
            config = {
                "type": self.types[mapping.protocol],
                "local_ip": f"{container.user_id}-{container.uuid}",
                "local_port": mapping.internal_port,
                "use_compression": "true",
            }
            if mapping.protocol == "http":
                config["subdomain"] = self._http_subdomain(container, mapping, http_count)
            else:
                if not mapping.public_port:
                    raise WhaleError(
                        f"No public port has been allocated for challenge port '{mapping.name}'."
                    )
                config["remote_port"] = mapping.public_port
            rules.append(self.FrpRule(rule_name, config))
        return rules

    def reload(self, exclude=None):
        rules = []
        for container in DBContainer.get_all_alive_container():
            if container.uuid == exclude:
                continue
            rules.extend(self._rules_for_container(container))

        try:
            if not self.common:
                common = get_config("whale:frp_config_template", "")
                if "[common]" in common:
                    self.common = common
                else:
                    remote = self.ses.get(f"{self.url}/api/config", timeout=5)
                    if remote.status_code != 200:
                        raise RequestException(
                            f"FRP config endpoint returned HTTP {remote.status_code}"
                        )
                    set_config("whale:frp_config_template", remote.text)
                    self.common = remote.text
            config = self.common + "\n" + "\n".join(str(rule) for rule in rules)
            update_response = self.ses.put(
                f"{self.url}/api/config", config, timeout=5
            )
            if update_response.status_code != 200:
                raise RequestException(
                    f"FRP config update returned HTTP {update_response.status_code}"
                )
            reload_response = self.ses.get(f"{self.url}/api/reload", timeout=5)
            if reload_response.status_code != 200:
                raise RequestException(
                    f"FRP reload returned HTTP {reload_response.status_code}"
                )
        except RequestException as exc:
            raise WhaleError(
                "\nfrpc request failed\n"
                + (f"{exc}\n" if str(exc) else "")
                + "please check the frp related configs"
            ) from None

    def access(self, container: WhaleContainer):
        mappings = list(container.port_mappings)
        if not mappings:
            return "<p>No ports are configured for this instance.</p>"
        http_count = sum(1 for mapping in mappings if mapping.protocol == "http")
        host = get_config("whale:frp_direct_ip_address", "")
        http_port = str(get_config("whale:frp_http_port", "80"))
        domain_suffix = get_config("whale:frp_http_domain_suffix", "")
        safe_host = html.escape(str(host))
        rows = []
        cards = []

        for mapping in mappings:
            name = html.escape(mapping.name)
            if mapping.protocol == "http":
                subdomain = self._http_subdomain(container, mapping, http_count)
                public_host = f"{subdomain}.{domain_suffix}"
                if http_port and http_port != "80":
                    href = f"http://{public_host}:{http_port}/"
                    host_display = f"{public_host}:{http_port}"
                else:
                    href = f"http://{public_host}/"
                    host_display = public_host
                cards.append(
                    f'<li><strong>{name}</strong> — '
                    f'<a target="_blank" rel="noopener noreferrer" href="{html.escape(href, quote=True)}">'
                    f'{html.escape(host_display)}</a> (HTTP)</li>'
                )
                rows.append((name, "HTTP", str(mapping.internal_port), host_display))
            elif mapping.protocol == "ssh":
                public_port = mapping.public_port
                user = html.escape(str(container.challenge.user or "user"))
                command = f"ssh {container.challenge.user or 'user'}@{safe_host} -p {public_port}"
                safe_command = html.escape(command)
                cards.append(
                    f'<li><strong>{name}</strong> — <code class="click-copy">{safe_command}</code>'
                    f' (SSH)</li>'
                )
                rows.append((name, "SSH", str(mapping.internal_port), str(public_port or "")))
            else:
                public_port = mapping.public_port
                command = f"nc {safe_host} {public_port}"
                safe_command = html.escape(command)
                cards.append(
                    f'<li><strong>{name}</strong> — <code class="click-copy">{safe_command}</code>'
                    f' (TCP)</li>'
                )
                rows.append((name, "TCP", str(mapping.internal_port), str(public_port or "")))

        challenge_data = {
            "User": container.challenge.user,
            "Password": container.challenge.password,
            "Direct host": safe_host,
        }
        detail_rows = []
        for key, value in challenge_data.items():
            if value is not None and str(value).strip():
                detail_rows.append(
                    f"<tr><td>{html.escape(key)}</td><td><code class=\"click-copy\">"
                    f"{html.escape(str(value))}</code></td></tr>"
                )

        return (
            '<div class="whale-port-access"><h6><u>Available services</u></h6><ul class="text-start">'
            + "".join(cards)
            + "</ul>"
            + (
                '<hr><table class="table table-bordered table-sm"><tr>'
                '<th>Key</th><th>Value</th></tr>' + "".join(detail_rows) + "</table>"
                if detail_rows else ""
            )
            + "</div>"
            + "<style>"
            + ".click-copy{cursor:pointer}.click-copy:hover{filter:brightness(50%)}"
            + ".text-start{text-align:start}"
            + "</style>"
            + "<script>(function(){document.querySelectorAll('.click-copy').forEach(function(el){"
              "if(!el.dataset.whaleCopyBound){el.dataset.whaleCopyBound='1';el.addEventListener('click',function(){"
              "navigator.clipboard.writeText(el.textContent).then(function(){"
              "CTFd._functions.events.eventAlert({title:'Success',"
              "html:'Content has been copied to your clipboard.',button:'OK'});"
              "}).catch(function(){CTFd._functions.events.eventAlert({title:'Copy failed',"
              "html:'Unable to copy to clipboard. Check browser permissions or use HTTPS.',button:'OK'});});"
              "});}});})();</script>"
        )

    def register(self, container: WhaleContainer):
        mappings = list(container.port_mappings)
        if not mappings:
            # Distinguish a genuinely unconfigured challenge from a deployment
            # where the updated DBContainer snapshot code has not been loaded.
            try:
                configured_count = len(container.challenge.ports)
            except Exception:
                configured_count = 0
            if configured_count:
                return False, (
                    "Port definitions exist for this challenge, but this instance has no port mappings. "
                    "Ensure the updated utils/db.py is deployed and restart all CTFd workers."
                )
            return False, (
                "No ports are configured for this challenge. Check that "
                "dynamic_docker_challenge_ports contains rows for this challenge ID."
            )

        cache = CacheProvider(app=current_app)
        allocated = []
        try:
            for mapping in mappings:
                if mapping.protocol in ("tcp", "ssh") and not mapping.public_port:
                    port = cache.get_available_port()
                    if not port:
                        # Discard uncommitted mappings first. Otherwise the ORM's
                        # identity map can make a reservation look persisted/in use.
                        db.session.rollback()
                        for allocated_port in allocated:
                            cache.add_available_port(allocated_port)
                        return False, "No available public ports. Please wait or increase the FRP port range."
                    mapping.public_port = int(port)
                    allocated.append(int(port))
            db.session.commit()
            self.reload()
            # The DB mappings are durable now, so the short-lived reservation
            # leases can be removed without making the ports available.
            for allocated_port in allocated:
                cache.commit_port_reservation(allocated_port)
            return True, "success"
        except Exception as exc:
            db.session.rollback()

            # Only free a port after FRP confirms its route is absent and the
            # persisted DB mapping has been cleared. If either step fails, the
            # database mapping keeps ownership of the port for later cleanup.
            routes_removed = False
            try:
                self.reload(exclude=container.uuid)
                routes_removed = True
            except Exception:
                logging.exception(
                    "Could not confirm removal of partially registered FRP routes "
                    "for instance %s-%s",
                    container.user_id,
                    container.uuid,
                )

            mappings_cleared = False
            if routes_removed:
                try:
                    for mapping in container.port_mappings:
                        if mapping.public_port in allocated:
                            mapping.public_port = None
                    db.session.commit()
                    mappings_cleared = True
                except Exception:
                    db.session.rollback()
                    logging.exception(
                        "Could not clear port mappings after FRP registration failure"
                    )

            for allocated_port in allocated:
                try:
                    if mappings_cleared:
                        cache.add_available_port(allocated_port)
                    else:
                        # The DB mapping still owns this port; release only the
                        # temporary lease so reconciliation can rely on the DB.
                        cache.commit_port_reservation(allocated_port)
                except Exception:
                    logging.exception(
                        "Could not reconcile public port %s after FRP registration failure",
                        allocated_port,
                    )

            logging.log(
                "whale", "Error registering multi-port FRP routes",
                name=container.user.name if container.user else str(container.user_id),
                challenge_id=container.challenge_id,
            )
            if not routes_removed:
                return False, (
                    "FRP registration failed and route removal could not be confirmed. "
                    "Allocated ports remain reserved; retry instance cleanup. "
                    f"Details: {exc}"
                )
            if not mappings_cleared:
                return False, (
                    "FRP registration failed and port mappings could not be cleared. "
                    "Allocated ports remain reserved for safety. "
                    f"Details: {exc}"
                )
            return False, f"Unable to register FRP routes: {exc}"

    def unregister(self, container: WhaleContainer):
        try:
            # Reload first; only return ports to the pool once FRP no longer has routes.
            self.reload(exclude=container.uuid)
            # The controller releases allocated ports only after the Docker service
            # has also been removed successfully.
            return True, "success"
        except Exception as exc:
            logging.log(
                "whale", "Error unregistering multi-port FRP routes",
                name=container.user.name if container.user else str(container.user_id),
                challenge_id=container.challenge_id,
            )
            return False, f"Unable to unregister FRP routes: {exc}"

    def check_availability(self):
        try:
            response = self.ses.get(f"{self.url}/api/status", timeout=2.0)
        except RequestException:
            return False, "Unable to access frpc admin api"
        if response.status_code == 401:
            return False, "frpc admin api unauthorized"
        if not 200 <= response.status_code < 300:
            return False, f"frpc admin api returned HTTP {response.status_code}"
        return True, "Available"
