import random
import uuid
from datetime import datetime

from jinja2 import Template
from sqlalchemy import UniqueConstraint

from CTFd.utils import get_config
from CTFd.models import db
from CTFd.plugins.dynamic_challenges import DynamicChallenge


class WhaleConfig(db.Model):
    key = db.Column(db.String(length=128), primary_key=True)
    value = db.Column(db.Text)

    def __init__(self, key, value):
        self.key = key
        self.value = value

    def __repr__(self):
        return "<WhaleConfig {0} {1}>".format(self.key, self.value)


class WhaleRedirectTemplate(db.Model):
    key = db.Column(db.String(20), primary_key=True)
    frp_template = db.Column(db.Text)
    access_template = db.Column(db.Text)

    def __init__(self, key, access_template, frp_template):
        self.key = key
        self.access_template = access_template
        self.frp_template = frp_template

    def __repr__(self):
        return "<WhaleRedirectTemplate {0}>".format(self.key)


class DynamicDockerChallenge(DynamicChallenge):
    __mapper_args__ = {"polymorphic_identity": "dynamic_docker"}
    id = db.Column(
        db.Integer,
        db.ForeignKey("dynamic_challenge.id", ondelete="CASCADE"),
        primary_key=True,
    )

    memory_limit = db.Column(db.Text, default="128m")
    cpu_limit = db.Column(db.Float, default=0.5)
    dynamic_score = db.Column(db.Integer, default=0)

    # Kept for model/API compatibility. Multi-port forwarding uses the child
    # table below, so no existing table needs a new column.
    docker_image = db.Column(db.Text, default=0)
    redirect_type = db.Column(db.Text, default=0)
    user = db.Column(db.Text, nullable=True)
    password = db.Column(db.Text, nullable=True)
    redirect_port = db.Column(db.Integer, default=0)

    @property
    def ports(self):
        # Keep the port definitions in a separate table and query explicitly.
        # This also avoids introducing a joined-inheritance relationship between
        # the port table and CTFd's polymorphic challenge model.
        return DynamicDockerChallengePort.query.filter_by(
            challenge_id=self.id
        ).order_by(DynamicDockerChallengePort.position).all()

    def __init__(self, *args, **kwargs):
        kwargs["initial"] = kwargs["value"]
        super(DynamicDockerChallenge, self).__init__(**kwargs)


class DynamicDockerChallengePort(db.Model):
    """Port/protocol settings belonging to one dynamic Docker challenge."""

    __tablename__ = "dynamic_docker_challenge_ports"
    __table_args__ = (
        UniqueConstraint("challenge_id", "name", name="uq_whale_challenge_port_name"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    challenge_id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(32), nullable=False)
    protocol = db.Column(db.String(8), nullable=False)
    internal_port = db.Column(db.Integer, nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return "<DynamicDockerChallengePort {0}:{1}/{2}>".format(
            self.name, self.internal_port, self.protocol
        )


class WhaleContainer(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(None, db.ForeignKey("users.id"))
    challenge_id = db.Column(None, db.ForeignKey("challenges.id"))
    start_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    renew_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.Integer, default=1)
    uuid = db.Column(db.String(256))
    node = db.Column(db.String(64), nullable=True)
    # Legacy single-port field remains available but new forwarding uses
    # WhaleContainerPort.public_port, one row per configured port.
    port = db.Column(db.Integer, nullable=True, default=0)
    flag = db.Column(db.String(128), nullable=False)

    user = db.relationship("Users", foreign_keys="WhaleContainer.user_id", lazy="select")
    challenge = db.relationship(
        "DynamicDockerChallenge", foreign_keys="WhaleContainer.challenge_id", lazy="select"
    )
    port_mappings = db.relationship(
        "WhaleContainerPort",
        back_populates="container",
        cascade="all, delete-orphan",
        order_by="WhaleContainerPort.position",
        lazy="select",
    )

    @property
    def http_subdomain(self):
        return Template(
            get_config("whale:template_http_subdomain", "{{ container.uuid }}")
        ).render(container=self)

    def __init__(self, user_id, challenge_id):
        self.user_id = user_id
        self.challenge_id = challenge_id
        self.start_time = datetime.now()
        self.renew_count = 0
        self.uuid = str(uuid.uuid4())
        self.flag = Template(
            get_config(
                "whale:template_chall_flag",
                '{{ "flag{"+uuid.uuid4()|string+"}" }}',
            )
        ).render(container=self, uuid=uuid, random=random, get_config=get_config)

    @property
    def user_access(self):
        from .utils.routers import Router

        return Router.access(self)

    @property
    def frp_config(self):
        # Retain the old property for extensions that still reference it.
        template = WhaleRedirectTemplate.query.filter_by(
            key=self.challenge.redirect_type
        ).first()
        if template is None:
            return ""
        return Template(template.frp_template).render(container=self, get_config=get_config)

    def __repr__(self):
        return "<WhaleContainer ID:{0} {1} {2} {3} {4}>".format(
            self.id, self.user_id, self.challenge_id, self.start_time, self.renew_count
        )


class WhaleContainerPort(db.Model):
    """Runtime snapshot of a challenge port, including its allocated public port."""

    __tablename__ = "whale_container_ports"
    __table_args__ = (
        UniqueConstraint("container_id", "name", name="uq_whale_container_port_name"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    container_id = db.Column(
        db.Integer, db.ForeignKey("whale_container.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(32), nullable=False)
    protocol = db.Column(db.String(8), nullable=False)
    internal_port = db.Column(db.Integer, nullable=False)
    public_port = db.Column(db.Integer, nullable=True)
    position = db.Column(db.Integer, nullable=False, default=0)

    container = db.relationship("WhaleContainer", back_populates="port_mappings")

    def __repr__(self):
        return "<WhaleContainerPort {0}:{1}/{2} -> {3}>".format(
            self.name, self.internal_port, self.protocol, self.public_port
        )
