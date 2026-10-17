from types import SimpleNamespace

from flask import Blueprint

from CTFd.models import db, Flags
from CTFd.plugins.challenges import BaseChallenge
try:
    from CTFd.plugins.challenges import ChallengeCreateException, ChallengeUpdateException
except ImportError:  # Compatibility with older CTFd releases
    class ChallengeCreateException(ValueError):
        pass
    class ChallengeUpdateException(ValueError):
        pass
from CTFd.plugins.dynamic_challenges import DynamicValueChallenge
from CTFd.plugins.flags import get_flag_class
from CTFd.utils import user as current_user

from .models import WhaleContainer, DynamicDockerChallenge
from .utils.control import ControlUtil
from .utils.ports import parse_ports_json, replace_challenge_ports, serialize_challenge_ports


class DynamicValueDockerChallenge(BaseChallenge):
    id = "dynamic_docker"
    name = "dynamic_docker"
    blueprint = Blueprint(
        "ctfd-whale-challenge",
        __name__,
        template_folder="templates",
        static_folder="assets",
    )
    challenge_model = DynamicDockerChallenge

    @classmethod
    def create(cls, request):
        data = dict(request.form or request.get_json() or {})
        raw_ports = data.pop("ports_json", data.pop("ports", None))
        try:
            ports = parse_ports_json(raw_ports)
        except ValueError as exc:
            raise ChallengeCreateException(str(exc)) from exc

        # BaseChallenge.create expects a request-like object and passes its data
        # to the SQLAlchemy model. Keep the auxiliary ports JSON out of that data.
        sanitized_request = SimpleNamespace(form=data, get_json=lambda: data)
        challenge = super().create(sanitized_request)
        try:
            replace_challenge_ports(challenge, ports)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return challenge

    @classmethod
    def read(cls, challenge):
        challenge = DynamicDockerChallenge.query.filter_by(id=challenge.id).first()
        return {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "initial": challenge.initial,
            "decay": challenge.decay,
            "minimum": challenge.minimum,
            "description": challenge.description,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "ports": serialize_challenge_ports(challenge),
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }

    @classmethod
    def update(cls, challenge, request):
        data = dict(request.form or request.get_json() or {})
        raw_ports = data.pop("ports_json", data.pop("ports", None))
        try:
            ports = parse_ports_json(raw_ports) if raw_ports is not None else None
        except ValueError as exc:
            raise ChallengeUpdateException(str(exc)) from exc

        for attr, value in data.items():
            if attr in ("initial", "minimum", "decay"):
                value = float(value)
            if attr == "dynamic_score":
                value = int(value)
            setattr(challenge, attr, value)

        if ports is not None:
            replace_challenge_ports(challenge, ports)

        db.session.commit()
        if challenge.dynamic_score == 1:
            return DynamicValueChallenge.calculate_value(challenge)
        return challenge

    @classmethod
    def attempt(cls, challenge, request):
        data = request.form or request.get_json()
        submission = data["submission"].strip()
        flags = Flags.query.filter_by(challenge_id=challenge.id).all()

        if flags:
            for flag in flags:
                if get_flag_class(flag.type).compare(flag, submission):
                    return True, "Correct"
            return False, "Incorrect"

        user_id = current_user.get_current_user().id
        records = WhaleContainer.query.filter_by(
            user_id=user_id, challenge_id=challenge.id
        ).all()
        if not records:
            return False, "Please solve it during the container is running"
        if records[0].flag == submission:
            return True, "Correct"
        return False, "Incorrect"

    @classmethod
    def solve(cls, user, team, challenge, request):
        super().solve(user, team, challenge, request)
        if challenge.dynamic_score == 1:
            DynamicValueChallenge.calculate_value(challenge)

    @classmethod
    def delete(cls, challenge):
        for container in WhaleContainer.query.filter_by(challenge_id=challenge.id).all():
            ControlUtil.try_remove_container(container.user_id)
        # Child table has no relationship to CTFd's joined-inheritance model;
        # delete its rows explicitly before deleting the challenge.
        from .models import DynamicDockerChallengePort
        DynamicDockerChallengePort.query.filter_by(challenge_id=challenge.id).delete(
            synchronize_session=False
        )
        db.session.commit()
        super().delete(challenge)
