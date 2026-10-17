import datetime

from CTFd.models import db
from CTFd.utils import get_config
from ..models import (
    DynamicDockerChallengePort,
    WhaleContainer,
    WhaleContainerPort,
    WhaleRedirectTemplate,
)


class DBContainer:
    @staticmethod
    def create_container_record(user_id, challenge_id):
        """Create the runtime instance and snapshot its challenge's port definitions.

        Challenge definitions and instance mappings intentionally live in separate
        tables. The latter includes a public port allocated for this specific run.
        """
        try:
            normalized_challenge_id = int(challenge_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid challenge ID; cannot load port definitions.") from exc

        definitions = (
            DynamicDockerChallengePort.query
            .filter_by(challenge_id=normalized_challenge_id)
            .order_by(DynamicDockerChallengePort.position, DynamicDockerChallengePort.id)
            .all()
        )
        if not definitions:
            raise ValueError(
                "No ports are configured for this challenge. No rows were found in "
                f"dynamic_docker_challenge_ports for challenge_id={normalized_challenge_id}."
            )

        container = WhaleContainer(
            user_id=user_id,
            challenge_id=normalized_challenge_id,
        )
        db.session.add(container)
        try:
            # Populate the relationship itself, rather than only inserting rows by
            # container_id. This guarantees the in-memory collection consumed by
            # Router.register() is populated immediately after creation.
            for definition in definitions:
                container.port_mappings.append(
                    WhaleContainerPort(
                        name=definition.name,
                        protocol=definition.protocol,
                        internal_port=definition.internal_port,
                        public_port=None,
                        position=definition.position,
                    )
                )
            if not container.port_mappings:
                raise RuntimeError(
                    "Port definitions were found but the instance port mappings are empty."
                )
            db.session.commit()
            return container
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def add_container_node(container, node):
        container.node = node
        db.session.add(container)
        db.session.commit()
        return container

    @staticmethod
    def get_current_containers(user_id):
        q = db.session.query(WhaleContainer)
        q = q.filter(WhaleContainer.user_id == user_id)
        return q.first()

    @staticmethod
    def get_container_by_port(port):
        mapping = WhaleContainerPort.query.filter_by(public_port=port).first()
        if mapping:
            return mapping.container
        return WhaleContainer.query.filter_by(port=port).first()

    @staticmethod
    def remove_container_record(user_id):
        containers = WhaleContainer.query.filter_by(user_id=user_id).all()
        container_ids = [container.id for container in containers]
        if container_ids:
            WhaleContainerPort.query.filter(
                WhaleContainerPort.container_id.in_(container_ids)
            ).delete(synchronize_session=False)
        WhaleContainer.query.filter_by(user_id=user_id).delete(synchronize_session=False)
        db.session.commit()

    @staticmethod
    def get_all_expired_container():
        timeout = int(get_config("whale:docker_timeout", "3600"))
        q = db.session.query(WhaleContainer)
        q = q.filter(
            WhaleContainer.start_time
            < datetime.datetime.now() - datetime.timedelta(seconds=timeout)
        )
        return q.all()

    @staticmethod
    def get_all_alive_container():
        timeout = int(get_config("whale:docker_timeout", "3600"))
        q = db.session.query(WhaleContainer)
        q = q.filter(
            WhaleContainer.start_time
            >= datetime.datetime.now() - datetime.timedelta(seconds=timeout)
        )
        return q.all()

    @staticmethod
    def get_all_container():
        return db.session.query(WhaleContainer).all()

    @staticmethod
    def get_all_alive_container_page(page_start, page_end):
        timeout = int(get_config("whale:docker_timeout", "3600"))
        q = db.session.query(WhaleContainer)
        q = q.filter(
            WhaleContainer.start_time
            >= datetime.datetime.now() - datetime.timedelta(seconds=timeout)
        )
        return q.slice(page_start, page_end).all()

    @staticmethod
    def get_all_alive_container_count():
        timeout = int(get_config("whale:docker_timeout", "3600"))
        q = db.session.query(WhaleContainer)
        q = q.filter(
            WhaleContainer.start_time
            >= datetime.datetime.now() - datetime.timedelta(seconds=timeout)
        )
        return q.count()


class DBRedirectTemplate:
    @staticmethod
    def get_all_templates():
        return WhaleRedirectTemplate.query.all()

    @staticmethod
    def create_template(name, access_template, frp_template):
        if WhaleRedirectTemplate.query.filter_by(key=name).first():
            return
        db.session.add(WhaleRedirectTemplate(name, access_template, frp_template))
        db.session.commit()

    @staticmethod
    def delete_template(name):
        WhaleRedirectTemplate.query.filter_by(key=name).delete()
        db.session.commit()
