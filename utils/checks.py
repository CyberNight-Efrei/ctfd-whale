import warnings

from docker.errors import DockerException, TLSParameterError, APIError, requests

from CTFd.utils import get_config

from .docker import get_docker_client
from .routers import Router, _routers


class WhaleChecks:
    @staticmethod
    def check_docker_api():
        try:
            client = get_docker_client()
        except TLSParameterError as e:
            return f'Docker TLS Parameters incorrect ({e})'
        except DockerException as e:
            return f'Docker API url incorrect ({e})'
        try:
            client.ping()
        except (APIError, requests.RequestException):
            return f'Unable to connect to Docker API, check your API connectivity'

        docker_api_url = str(
            get_config("whale:docker_api_url", "unix:///var/run/docker.sock")
        )
        docker_use_ssl = get_config("whale:docker_use_ssl", False)
        if isinstance(docker_use_ssl, str):
            docker_use_ssl = docker_use_ssl.strip().lower() in ("1", "true", "yes", "on")
        if docker_api_url.startswith("tcp://") and not docker_use_ssl:
            warnings.warn(
                "CTFd-Whale is configured to use the Docker TCP API without TLS. "
                "Prefer unix:///var/run/docker.sock for a local Docker daemon; "
                "never expose an unauthenticated Docker API on an untrusted network.",
                RuntimeWarning,
            )

        credentials = get_config("whale:docker_credentials")
        if credentials and credentials.count(':') == 1:
            try:
                client.login(*credentials.split(':'))
            except DockerException:
                return f'Unable to log into docker registry, check your credentials'
        swarm = client.info()['Swarm']
        if not swarm['ControlAvailable']:
            return f'Docker swarm not available. You should initialize a swarm first. ($ docker swarm init)'

    @staticmethod
    def check_frp_connection():
        router_conftype = get_config("whale:router_type", "frp")
        if router_conftype not in _routers:
            return "invalid router type: " + router_conftype
        ok, msg = _routers[router_conftype]().check_availability()
        if not ok:
            return msg

    @staticmethod
    def perform():
        errors = []
        for attr in dir(WhaleChecks):
            if attr.startswith('check_'):
                err = getattr(WhaleChecks, attr)()
                if err:
                    errors.append(err)
        return errors
