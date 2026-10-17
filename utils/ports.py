"""Helpers for validating and persisting per-challenge port definitions."""

import json
import re

from CTFd.models import db

from ..models import DynamicDockerChallengePort


ALLOWED_PROTOCOLS = {"http", "tcp", "ssh"}
MAX_PORTS_PER_CHALLENGE = 20
PORT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def parse_ports_json(raw):
    """Return normalized port definitions or raise ValueError with a useful message."""
    if raw is None or raw == "":
        raise ValueError("At least one port must be configured.")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Port configuration must be valid JSON.") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("At least one port must be configured.")
    if len(raw) > MAX_PORTS_PER_CHALLENGE:
        raise ValueError("A challenge can expose at most 20 ports.")

    result = []
    seen_names = set()
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("Every port definition must be an object.")

        name = str(item.get("name", "")).strip().lower()
        protocol = str(item.get("protocol", "")).strip().lower()
        raw_port = item.get("internal_port")

        if not PORT_NAME_RE.fullmatch(name):
            raise ValueError(
                "Port names must be 1-32 characters, start with a letter or digit, "
                "and contain only lowercase letters, digits, '_' or '-'."
            )
        if name in seen_names:
            raise ValueError("Port names must be unique within a challenge.")
        seen_names.add(name)

        if protocol not in ALLOWED_PROTOCOLS:
            raise ValueError("Port protocol must be one of: HTTP, TCP or SSH.")
        try:
            internal_port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("Every internal port must be an integer between 1 and 65535.") from exc
        if not 1 <= internal_port <= 65535:
            raise ValueError("Every internal port must be between 1 and 65535.")

        result.append(
            {
                "name": name,
                "protocol": protocol,
                "internal_port": internal_port,
                "position": position,
            }
        )
    return result


def replace_challenge_ports(challenge, port_definitions):
    """Replace definitions using ORM relationships (no existing-table migration needed)."""
    DynamicDockerChallengePort.query.filter_by(challenge_id=challenge.id).delete(
        synchronize_session=False
    )
    for port in port_definitions:
        db.session.add(
            DynamicDockerChallengePort(
                challenge_id=challenge.id,
                name=port["name"],
                protocol=port["protocol"],
                internal_port=port["internal_port"],
                position=port["position"],
            )
        )
    db.session.flush()


def serialize_challenge_ports(challenge):
    return [
        {
            "name": port.name,
            "protocol": port.protocol,
            "internal_port": port.internal_port,
        }
        for port in challenge.ports
    ]
