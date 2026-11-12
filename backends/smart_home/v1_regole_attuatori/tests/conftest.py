"""HTTP-only support for the automation-rules black-box suite."""
import os
import re

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
SECOND_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")
USER_ONE_ID = 1
USER_TWO_ID = 2


def bearer(token: str) -> dict[str, str]:
    """Build the only authentication input accepted by private routes."""
    return {"Authorization": f"Bearer {token}"}


def assert_json(response: httpx.Response, status_code: int) -> dict:
    assert response.status_code == status_code, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return response.json()


def assert_error(response: httpx.Response, status_code: int) -> dict:
    body = assert_json(response, status_code)
    assert set(body) == {"detail", "status_code", "error_type"}
    assert body["status_code"] == status_code
    assert isinstance(body["detail"], str) and body["detail"]
    assert isinstance(body["error_type"], str) and body["error_type"]
    return body


def assert_location(response: httpx.Response, path: str) -> None:
    location = response.headers.get("location")
    assert location, "A successful create response must include Location"
    assert location.rstrip("/").endswith(path)


def assert_utc_second(value: str) -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", value)


def assert_actuator(body: dict) -> None:
    assert set(body) == {"id", "owner_id", "serial", "name", "type", "current_value"}
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body["owner_id"] in {USER_ONE_ID, USER_TWO_ID}
    assert re.fullmatch(r"[A-Z0-9-]{6,32}", body["serial"])
    assert 1 <= len(body["name"]) <= 80
    assert body["type"] in {"SWITCH", "DIMMER", "THERMOSTAT"}
    assert isinstance(body["current_value"], str)


def assert_rule(body: dict) -> None:
    assert set(body) == {
        "id", "owner_id", "name", "enabled", "source_kind", "comparator",
        "threshold", "cooldown_seconds", "actions",
    }
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body["owner_id"] in {USER_ONE_ID, USER_TWO_ID}
    assert 3 <= len(body["name"]) <= 80
    assert isinstance(body["enabled"], bool)
    assert body["source_kind"] in {"TEMPERATURE", "HUMIDITY", "MOTION"}
    assert body["comparator"] in {"GT", "GTE", "LT", "LTE", "EQ"}
    assert -50 <= body["threshold"] <= 5000
    assert 0 <= body["cooldown_seconds"] <= 86400
    assert 1 <= len(body["actions"]) <= 5
    assert [action["order"] for action in body["actions"]] == sorted(
        action["order"] for action in body["actions"]
    )
    assert len({action["actuator_id"] for action in body["actions"]}) == len(body["actions"])
    for action in body["actions"]:
        assert set(action) == {"id", "rule_id", "actuator_id", "order", "command_value"}
        assert action["rule_id"] == body["id"]
        assert 1 <= action["order"] <= 5


def assert_command(body: dict) -> None:
    assert set(body) == {
        "id", "actuator_id", "origin", "execution_id", "command_value", "status", "created_at",
    }
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body["origin"] in {"MANUAL", "RULE"}
    assert body["status"] in {"APPLIED", "SUPPRESSED"}
    assert_utc_second(body["created_at"])


def assert_execution(body: dict) -> None:
    assert set(body) == {"id", "rule_id", "event_id", "outcome", "executed_at"}
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body["outcome"] in {"APPLIED", "SUPPRESSED"}
    assert_utc_second(body["executed_at"])


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=20.0, follow_redirects=False) as value:
        yield value


@pytest.fixture(scope="session")
def admin_headers():
    return bearer(ADMIN_TOKEN)


@pytest.fixture(scope="session")
def user_headers():
    return bearer(USER_TOKEN)


@pytest.fixture(scope="session")
def other_user_headers():
    return bearer(SECOND_USER_TOKEN)


@pytest.fixture
def tagged_values(request):
    """Stable, per-test values satisfying the public serial/event grammars."""
    base = re.sub(r"[^A-Za-z0-9]", "", request.node.name).upper() or "CASE"

    def serial(index: int) -> str:
        return f"T-{base[:12]}-{base[-12:]}-{index:02d}"[:32]

    def name(kind: str, index: int) -> str:
        return f"{kind} {base[:55]} {index}"[:80]

    def event_id(index: int) -> str:
        return f"EV-{base[:48]}-{index:02d}"[:64]

    return {"serial": serial, "name": name, "event_id": event_id}


def _command_list(client, headers, actuator_id: int, **params) -> dict:
    body = assert_json(
        client.get(f"/actuators/{actuator_id}/commands", headers=headers, params=params), 200
    )
    assert set(body) == {"items", "next_after_id"}
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    for item in body["items"]:
        assert_command(item)
    return body


@pytest.fixture
def command_list(client):
    return _command_list


@pytest.fixture
def actuator_factory(client, admin_headers, tagged_values):
    sequence = 0

    def create(
        *, owner_id: int = USER_ONE_ID, actuator_type: str = "SWITCH",
        initial_value: str = "OFF", serial: str | None = None, name: str | None = None,
    ) -> dict:
        nonlocal sequence
        sequence += 1
        payload = {
            "owner_id": owner_id,
            "serial": serial or tagged_values["serial"](sequence),
            "name": name or tagged_values["name"]("Actuator", sequence),
            "type": actuator_type,
            "initial_value": initial_value,
        }
        response = client.post("/actuators", headers=admin_headers, json=payload)
        body = assert_json(response, 201)
        assert_location(response, f"/actuators/{body['id']}")
        assert_actuator(body)
        for key in ("owner_id", "serial", "name", "type"):
            assert body[key] == payload[key]
        assert body["current_value"] == initial_value
        persisted = assert_json(client.get(f"/actuators/{body['id']}", headers=admin_headers), 200)
        assert persisted == body
        return body

    return create


@pytest.fixture
def rule_factory(client, admin_headers, user_headers, tagged_values):
    sequence = 0

    def create(
        actuator: dict, *, actions: list[dict] | None = None, owner_id: int = USER_ONE_ID,
        as_admin: bool = False, name: str | None = None, source_kind: str = "TEMPERATURE",
        comparator: str = "GT", threshold: float = 20, cooldown_seconds: int = 0,
        enabled: bool | None = None,
    ) -> dict:
        nonlocal sequence
        sequence += 1
        payload = {
            "name": name or tagged_values["name"]("Rule", sequence),
            "source_kind": source_kind,
            "comparator": comparator,
            "threshold": threshold,
            "cooldown_seconds": cooldown_seconds,
            "actions": actions or [{"order": 1, "actuator_id": actuator["id"], "command_value": "ON"}],
        }
        headers = admin_headers if as_admin else user_headers
        if as_admin:
            payload["owner_id"] = owner_id
        response = client.post("/automation-rules", headers=headers, json=payload)
        body = assert_json(response, 201)
        assert_location(response, f"/automation-rules/{body['id']}")
        assert_rule(body)
        assert body["owner_id"] == owner_id
        assert body["name"] == payload["name"]
        assert body["source_kind"] == source_kind
        assert body["comparator"] == comparator
        assert body["threshold"] == threshold
        assert body["cooldown_seconds"] == cooldown_seconds
        if enabled is not None:
            assert body["enabled"] is enabled
        persisted = assert_json(client.get(f"/automation-rules/{body['id']}", headers=headers), 200)
        assert persisted == body
        return body

    return create
