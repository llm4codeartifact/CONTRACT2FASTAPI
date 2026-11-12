"""HTTP-only fixtures for the telemetry-range API."""
import os
import re

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
SECOND_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")
USER_ONE_ID, USER_TWO_ID = 1, 2


def bearer(token: str) -> dict[str, str]: return {"Authorization": f"Bearer {token}"}


def assert_json(response: httpx.Response, status: int) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return response.json()


def assert_status(response: httpx.Response, status: int) -> None:
    assert response.status_code == status, response.text


def assert_utc(value: str) -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", value)


def assert_sensor(body: dict) -> None:
    assert {"id", "owner_id", "serial", "name", "type", "created_at"}.issubset(body)
    assert body["owner_id"] in {USER_ONE_ID, USER_TWO_ID}
    assert re.fullmatch(r"[A-Z0-9-]{6,32}", body["serial"])
    assert body["type"] in {"TEMPERATURE", "HUMIDITY", "CO2", "ILLUMINANCE"}
    assert_utc(body["created_at"])


def assert_reading(body: dict) -> None:
    assert set(body) == {"id", "sensor_id", "value", "captured_at", "created_at"}
    assert body["id"] > 0; assert_utc(body["captured_at"]); assert_utc(body["created_at"])


def assert_policy(body: dict) -> None:
    assert {"id", "sensor_id", "lower_bound", "upper_bound", "created_at", "updated_at"}.issubset(body)
    assert body["lower_bound"] is not None or body["upper_bound"] is not None
    if body["lower_bound"] is not None and body["upper_bound"] is not None:
        assert body["lower_bound"] < body["upper_bound"]


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=20.0, follow_redirects=False) as value: yield value


@pytest.fixture(scope="session")
def admin_headers(): return bearer(ADMIN_TOKEN)


@pytest.fixture(scope="session")
def user_headers(): return bearer(USER_TOKEN)


@pytest.fixture(scope="session")
def other_user_headers(): return bearer(SECOND_USER_TOKEN)


@pytest.fixture
def values(request):
    raw = re.sub(r"[^A-Za-z0-9]", "", request.node.name).upper() or "CASE"
    def serial(n): return f"T-{raw[:12]}-{raw[-12:]}-{n:02d}"[:32]
    def name(n): return f"Sensor {raw[:65]} {n}"[:80]
    return {"serial": serial, "name": name}


@pytest.fixture
def sensor_factory(client, admin_headers, values):
    index = 0
    def create(*, owner_id=USER_ONE_ID, sensor_type="TEMPERATURE", serial=None, name=None):
        nonlocal index
        index += 1
        payload = {"owner_id": owner_id, "serial": serial or values["serial"](index), "name": name or values["name"](index), "type": sensor_type}
        response = client.post("/sensors", headers=admin_headers, json=payload)
        body = assert_json(response, 201)
        assert_sensor(body); assert response.headers.get("location", "").rstrip("/").endswith(f"/sensors/{body['id']}")
        assert body["owner_id"] == owner_id and body["type"] == sensor_type
        assert assert_json(client.get(f"/sensors/{body['id']}", headers=admin_headers), 200) == body
        return body
    return create


def readings(client, headers, sensor_id, **params):
    body = assert_json(client.get(f"/sensors/{sensor_id}/readings", headers=headers, params=params), 200)
    assert {"items", "next_after_id"}.issubset(body)
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    for item in body["items"]: assert_reading(item)
    return body


def alerts(client, headers, **params):
    body = assert_json(client.get("/alerts", headers=headers, params=params), 200)
    assert {"items", "next_after_id"}.issubset(body)
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    return body
