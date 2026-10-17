import datetime
import traceback

from flask import current_app

from CTFd.models import db
from CTFd.utils import get_config
from .cache import CacheProvider
from .db import DBContainer
from .docker import DockerUtils
from .routers import Router


class ControlUtil:
    @staticmethod
    def try_add_container(user_id, challenge_id):
        try:
            container = DBContainer.create_container_record(user_id, challenge_id)
        except ValueError as exc:
            return False, str(exc)
        except Exception:
            db.session.rollback()
            print(traceback.format_exc())
            return False, "Unable to prepare this challenge's port mappings. Check the CTFd-Whale logs."

        try:
            DockerUtils.add_container(container)
        except Exception:
            DBContainer.remove_container_record(user_id)
            print(traceback.format_exc())
            return False, "Docker Creation Error"

        ok, message = Router.register(container)
        if not ok:
            try:
                DockerUtils.remove_container(container)
            finally:
                DBContainer.remove_container_record(user_id)
            return False, message
        return True, "Container created"

    @staticmethod
    def _release_container_ports(container):
        """Return direct TCP/SSH ports after routes and Docker services are gone."""
        mappings = list(container.port_mappings)
        if not mappings:
            return
        cache = CacheProvider(app=current_app)
        released = []
        for mapping in mappings:
            if mapping.public_port:
                released.append(int(mapping.public_port))
                mapping.public_port = None
        db.session.commit()
        for port in released:
            cache.add_available_port(port)

    @staticmethod
    def try_remove_container(user_id):
        container = DBContainer.get_current_containers(user_id=user_id)
        if not container:
            return False, "No such container"
        for _ in range(3):
            try:
                ok, message = Router.unregister(container)
                if not ok:
                    return False, message
                DockerUtils.remove_container(container)
                ControlUtil._release_container_ports(container)
                DBContainer.remove_container_record(user_id)
                return True, "Container destroyed"
            except Exception:
                print(traceback.format_exc())
        return False, "Failed when destroying instance, please contact admin!"

    @staticmethod
    def try_renew_container(user_id):
        container = DBContainer.get_current_containers(user_id)
        if not container:
            return False, "No such container"
        timeout = int(get_config("whale:docker_timeout", "3600"))
        container.start_time = container.start_time + datetime.timedelta(seconds=timeout)
        if container.start_time > datetime.datetime.now():
            container.start_time = datetime.datetime.now()
        else:
            return False, "Invalid container"
        container.renew_count += 1
        db.session.commit()
        return True, "Container Renewed"
