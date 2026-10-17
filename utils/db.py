import datetime

from sqlalchemy import String, and_, cast, or_

from CTFd.models import Users, db
from CTFd.utils import get_config
from ..models import (
    DynamicDockerChallenge,
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
    def get_all_cleanup_candidates():
        """Find expired instances and stale in-progress lifecycle transitions."""
        now = datetime.datetime.now()
        timeout = int(get_config("whale:docker_timeout", "3600"))
        provisioning_timeout = max(
            60, int(get_config("whale:docker_provisioning_timeout", "600"))
        )
        expired_before = now - datetime.timedelta(seconds=timeout)
        stale_before = now - datetime.timedelta(seconds=provisioning_timeout)
        # Only stale teardown states are retried by age. A provisioning or
        # starting request may still be legitimately working; those states are
        # cleaned after the normal instance timeout if the worker crashed.
        transitional_states = (
            WhaleContainer.STATUS_STOPPING,
            WhaleContainer.STATUS_CLEANUP_PENDING,
        )
        q = db.session.query(WhaleContainer).filter(
            or_(
                WhaleContainer.start_time < expired_before,
                and_(
                    WhaleContainer.status.in_(transitional_states),
                    WhaleContainer.start_time < stale_before,
                ),
            )
        )
        return q.order_by(WhaleContainer.start_time.asc()).all()

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
    def get_routable_containers():
        """Instances whose FRP/TRP routes are meant to exist right now.

        New rows begin in PROVISIONING and may not have allocated public ports
        yet. STOPPING/CLEANUP_PENDING rows must not be reintroduced by a router
        reload. Existing rows with a NULL status remain routable for compatibility.
        """
        timeout = int(get_config("whale:docker_timeout", "3600"))
        alive_since = datetime.datetime.now() - datetime.timedelta(seconds=timeout)
        q = db.session.query(WhaleContainer).filter(
            WhaleContainer.start_time >= alive_since,
            or_(
                WhaleContainer.status.is_(None),
                WhaleContainer.status.in_(
                    (
                        WhaleContainer.STATUS_READY,
                        WhaleContainer.STATUS_STARTING,
                    )
                ),
            ),
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

    @staticmethod
    def get_admin_instance_page(
        page=1, per_page=20, search="", node="", challenge_id="", protocol=""
    ):
        """Return a filtered, paginated set of live instances for the admin UI.

        Filtering happens in SQL before pagination, so a search spans every page
        rather than only the rows currently rendered in the browser.
        """
        timeout = int(get_config("whale:docker_timeout", "3600"))
        alive_since = datetime.datetime.now() - datetime.timedelta(seconds=timeout)
        visible_filter = or_(
            WhaleContainer.start_time >= alive_since,
            WhaleContainer.status.in_(
                (
                    WhaleContainer.STATUS_STOPPING,
                    WhaleContainer.STATUS_CLEANUP_PENDING,
                )
            ),
        )
        q = db.session.query(WhaleContainer).filter(visible_filter)

        search = (search or "").strip()
        if search:
            pattern = f"%{search}%"
            search_conditions = [
                WhaleContainer.uuid.ilike(pattern),
                WhaleContainer.node.ilike(pattern),
                WhaleContainer.user.has(Users.name.ilike(pattern)),
                WhaleContainer.challenge.has(DynamicDockerChallenge.name.ilike(pattern)),
                WhaleContainer.port_mappings.any(
                    or_(
                        WhaleContainerPort.name.ilike(pattern),
                        cast(WhaleContainerPort.internal_port, String).ilike(pattern),
                        cast(WhaleContainerPort.public_port, String).ilike(pattern),
                    )
                ),
            ]
            if search.isdigit():
                numeric_search = int(search)
                search_conditions.extend([
                    WhaleContainer.id == numeric_search,
                    WhaleContainer.user_id == numeric_search,
                    WhaleContainer.challenge_id == numeric_search,
                ])
            q = q.filter(or_(*search_conditions))

        if node:
            q = q.filter(WhaleContainer.node == node)

        if challenge_id:
            try:
                q = q.filter(WhaleContainer.challenge_id == int(challenge_id))
            except (TypeError, ValueError):
                # Ignore malformed filter values rather than breaking the page.
                pass

        if protocol in ("http", "tcp", "ssh"):
            q = q.filter(
                WhaleContainer.port_mappings.any(WhaleContainerPort.protocol == protocol)
            )

        total = q.count()
        pages = (total + per_page - 1) // per_page if total else 0
        offset = max(page - 1, 0) * per_page
        items = (
            q.order_by(WhaleContainer.start_time.desc(), WhaleContainer.id.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )
        return items, total, pages

    @staticmethod
    def get_admin_instance_filter_options():
        """Get filter options from instances that have not yet expired."""
        timeout = int(get_config("whale:docker_timeout", "3600"))
        alive_since = datetime.datetime.now() - datetime.timedelta(seconds=timeout)
        alive_filter = or_(
            WhaleContainer.start_time >= alive_since,
            WhaleContainer.status.in_(
                (
                    WhaleContainer.STATUS_STOPPING,
                    WhaleContainer.STATUS_CLEANUP_PENDING,
                )
            ),
        )

        nodes = [
            row[0] for row in
            db.session.query(WhaleContainer.node)
            .filter(alive_filter, WhaleContainer.node.isnot(None), WhaleContainer.node != "")
            .distinct()
            .order_by(WhaleContainer.node)
            .all()
        ]

        challenges = (
            db.session.query(DynamicDockerChallenge.id, DynamicDockerChallenge.name)
            .join(WhaleContainer, WhaleContainer.challenge_id == DynamicDockerChallenge.id)
            .filter(alive_filter)
            .distinct()
            .order_by(DynamicDockerChallenge.name)
            .all()
        )
        return nodes, challenges


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
