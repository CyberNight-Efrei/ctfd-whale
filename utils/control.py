import datetime
import logging
import traceback

from flask import current_app

from CTFd.models import db
from CTFd.utils import get_config
from .cache import CacheProvider
from .db import DBContainer
from .docker import DockerUtils
from .routers import Router
from ..models import WhaleContainer


class ControlUtil:
    @staticmethod
    def _mark_cleanup_pending(container):
        """Keep a failed instance discoverable by the periodic cleanup job."""
        timeout = int(get_config("whale:docker_timeout", "3600"))
        try:
            container.start_time = datetime.datetime.now() - datetime.timedelta(
                seconds=timeout + 1
            )
            container.status = WhaleContainer.STATUS_CLEANUP_PENDING
            db.session.add(container)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.exception(
                "Unable to mark Whale instance %s-%s for cleanup",
                container.user_id,
                container.uuid,
            )

    @staticmethod
    def _remove_docker_resources(container, attempts=3):
        last_error = None
        for _ in range(attempts):
            try:
                DockerUtils.remove_container(container)
                return True, ""
            except Exception as exc:
                last_error = str(exc)
                logging.exception(
                    "Docker cleanup failed for Whale instance %s-%s",
                    container.user_id,
                    container.uuid,
                )
        return False, last_error or "Docker cleanup failed"

    @staticmethod
    def _unregister_routes(container, attempts=3):
        last_message = "Router cleanup failed"
        for _ in range(attempts):
            try:
                ok, message = Router.unregister(container)
                if ok:
                    return True, message
                last_message = message or last_message
            except Exception as exc:
                last_message = str(exc) or last_message
                logging.exception(
                    "Route cleanup failed for Whale instance %s-%s",
                    container.user_id,
                    container.uuid,
                )
        return False, last_message

    @staticmethod
    def _release_container_ports(container):
        """Return direct TCP/SSH ports only after routes and Docker services are gone."""
        mappings = list(container.port_mappings)
        if not mappings:
            return
        cache = CacheProvider(app=current_app)
        released = []
        for mapping in mappings:
            if mapping.public_port:
                released.append(int(mapping.public_port))
                mapping.public_port = None
        # Clear persisted ownership before making any port available to another
        # request. If Redis is unavailable, pool reconciliation can recover it.
        db.session.commit()
        for port in released:
            cache.add_available_port(port)

    @staticmethod
    def try_add_container(user_id, challenge_id):
        lifecycle_lock = CacheProvider(app=current_app, user_id=user_id)
        try:
            if not lifecycle_lock.acquire_lifecycle_lock(user_id):
                return False, "Another instance operation for this user is already in progress."
        except Exception:
            logging.exception("Unable to acquire lifecycle lock for user %s", user_id)
            return False, "Unable to safely start an instance right now. Please retry."

        try:
            return ControlUtil._try_add_container_locked(user_id, challenge_id)
        finally:
            try:
                lifecycle_lock.release_lifecycle_lock()
            except Exception:
                logging.exception("Unable to release lifecycle lock for user %s", user_id)

    @staticmethod
    def _try_add_container_locked(user_id, challenge_id):
        if DBContainer.get_current_containers(user_id=user_id):
            return False, (
                "An instance already exists for this user. Clean it up before starting another."
            )
        try:
            container = DBContainer.create_container_record(user_id, challenge_id)
        except ValueError as exc:
            return False, str(exc)
        except Exception:
            db.session.rollback()
            logging.exception("Unable to prepare port mappings for Whale instance")
            return False, "Unable to prepare this challenge's port mappings. Check the CTFd-Whale logs."

        try:
            DockerUtils.add_container(container)
        except Exception:
            logging.error("Docker creation failed:\n%s", traceback.format_exc())
            removed, cleanup_error = ControlUtil._remove_docker_resources(container)
            if removed:
                try:
                    DBContainer.remove_container_record(user_id)
                except Exception:
                    db.session.rollback()
                    ControlUtil._mark_cleanup_pending(container)
                    logging.exception("Could not remove DB record after failed instance creation")
                    return False, (
                        "Docker creation failed; cleanup is pending because the database "
                        "record could not be removed."
                    )
                return False, "Docker Creation Error"

            ControlUtil._mark_cleanup_pending(container)
            return False, (
                "Docker creation failed and some resources may remain. Cleanup has been "
                f"scheduled for retry. Details: {cleanup_error}"
            )

        try:
            container.status = WhaleContainer.STATUS_STARTING
            # Start the challenge lifetime after Docker provisioning, not while
            # images are still being pulled and services are being created.
            container.start_time = datetime.datetime.now()
            db.session.commit()
            ok, message = Router.register(container)
        except Exception as exc:
            db.session.rollback()
            logging.exception(
                "Router registration or lifecycle transition failed for Whale instance %s-%s",
                container.user_id,
                container.uuid,
            )
            ok, message = False, str(exc) or "Router registration failed"

        if ok:
            try:
                container.status = WhaleContainer.STATUS_READY
                db.session.commit()
            except Exception:
                db.session.rollback()
                logging.exception(
                    "Unable to persist ready state for Whale instance %s-%s",
                    container.user_id,
                    container.uuid,
                )
                ok = False
                message = "The instance was provisioned, but its final lifecycle state could not be saved."
        if ok:
            return True, "Container created"

        # Route cleanup and Docker cleanup are attempted independently. A router
        # outage must not leave challenge workloads running indefinitely.
        routes_removed, route_error = ControlUtil._unregister_routes(container)
        docker_removed, docker_error = ControlUtil._remove_docker_resources(container)
        if routes_removed and docker_removed:
            try:
                ControlUtil._release_container_ports(container)
                DBContainer.remove_container_record(user_id)
            except Exception:
                db.session.rollback()
                ControlUtil._mark_cleanup_pending(container)
                logging.exception(
                    "Could not finalize cleanup after router registration failure"
                )
                return False, (
                    f"{message}. The instance cleanup is pending and will be retried."
                )
            return False, message

        ControlUtil._mark_cleanup_pending(container)
        failures = []
        if not routes_removed:
            failures.append(f"route cleanup: {route_error}")
        if not docker_removed:
            failures.append(f"Docker cleanup: {docker_error}")
        return False, (
            f"{message}. Cleanup is pending and will be retried automatically "
            f"({'; '.join(failures)})."
        )

    @staticmethod
    def try_remove_container(user_id):
        lifecycle_lock = CacheProvider(app=current_app, user_id=user_id)
        try:
            if not lifecycle_lock.acquire_lifecycle_lock(user_id):
                return False, "Another instance operation is in progress. Please retry shortly."
        except Exception:
            logging.exception("Unable to acquire lifecycle lock for user %s", user_id)
            return False, "Unable to safely stop this instance right now. Please retry."

        try:
            return ControlUtil._try_remove_container_locked(user_id)
        finally:
            try:
                lifecycle_lock.release_lifecycle_lock()
            except Exception:
                logging.exception("Unable to release lifecycle lock for user %s", user_id)

    @staticmethod
    def _try_remove_container_locked(user_id):
        container = DBContainer.get_current_containers(user_id=user_id)
        if not container:
            return False, "No such container"

        # Persist intent before making external changes. If the worker crashes,
        # the stale STOPPING state lets the janitor resume cleanup later.
        try:
            container.status = WhaleContainer.STATUS_STOPPING
            # Give a crashed teardown its own grace period before the janitor
            # retries, instead of treating the original instance age as teardown age.
            container.start_time = datetime.datetime.now()
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.exception(
                "Unable to persist stopping state for Whale instance %s-%s",
                container.user_id,
                container.uuid,
            )

        # Do not stop after a route error: Docker resources should still be
        # removed when possible. Keep the DB record and public-port ownership if
        # either side cannot confirm cleanup, so retries remain safe.
        routes_removed, route_error = ControlUtil._unregister_routes(container)
        docker_removed, docker_error = ControlUtil._remove_docker_resources(container)

        if not routes_removed or not docker_removed:
            ControlUtil._mark_cleanup_pending(container)
            failures = []
            if not routes_removed:
                failures.append(f"route cleanup: {route_error}")
            if not docker_removed:
                failures.append(f"Docker cleanup: {docker_error}")
            return False, (
                "Cleanup is incomplete; the instance has been queued for automatic "
                f"retry ({'; '.join(failures)})."
            )

        try:
            ControlUtil._release_container_ports(container)
            DBContainer.remove_container_record(user_id)
            return True, "Container destroyed"
        except Exception:
            db.session.rollback()
            ControlUtil._mark_cleanup_pending(container)
            logging.exception(
                "Could not finalize removal of Whale instance %s-%s",
                container.user_id,
                container.uuid,
            )
            return False, (
                "Docker resources were removed, but final cleanup is pending and "
                "will be retried automatically."
            )

    @staticmethod
    def try_renew_container(user_id):
        lifecycle_lock = CacheProvider(app=current_app, user_id=user_id)
        try:
            if not lifecycle_lock.acquire_lifecycle_lock(user_id):
                return False, "Another instance operation is in progress. Please retry shortly."
        except Exception:
            logging.exception("Unable to acquire lifecycle lock for user %s", user_id)
            return False, "Unable to safely renew this instance right now. Please retry."

        try:
            return ControlUtil._try_renew_container_locked(user_id)
        finally:
            try:
                lifecycle_lock.release_lifecycle_lock()
            except Exception:
                logging.exception("Unable to release lifecycle lock for user %s", user_id)

    @staticmethod
    def _try_renew_container_locked(user_id):
        container = DBContainer.get_current_containers(user_id)
        if not container:
            return False, "No such container"
        if container.lifecycle_state != "ready":
            return False, "Instance is not in a ready state and cannot be renewed."
        timeout = int(get_config("whale:docker_timeout", "3600"))
        renewed_start_time = container.start_time + datetime.timedelta(seconds=timeout)
        if renewed_start_time <= datetime.datetime.now():
            return False, "Invalid container"
        container.start_time = datetime.datetime.now()
        container.renew_count += 1
        db.session.commit()
        return True, "Container Renewed"
