"""HTTP-only fixtures for hubs, rooms, and devices."""
import os
import re

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
SECOND_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")
USER_ONE_ID, USER_TWO_ID = 1, 2


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_json(response: httpx.Response, status: int) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return response.json()


def assert_error(response: httpx.Response, status: int) -> dict:
    body = assert_json(response, status)
    assert set(body) == {"detail", "status_code", "error_type"}
    assert body["status_code"] == status
    return body


def assert_etag(response: httpx.Response, body: dict) -> str:
    etag = response.headers.get("etag")
    assert etag and re.fullmatch(r'"[1-9][0-9]*"', etag)
    assert body["etag"] == etag
    return etag


def assert_location(response: httpx.Response, path: str) -> None:
    assert response.headers.get("location", "").rstrip("/").endswith(path)


def assert_hub(body: dict) -> None:
    assert set(body) == {"id", "owner_id", "serial", "name", "timezone", "revision", "etag"}
    assert body["owner_id"] in {USER_ONE_ID, USER_TWO_ID}
    assert re.fullmatch(r"[A-Z0-9-]{8,32}", body["serial"])
    assert 1 <= len(body["name"]) <= 80
    assert body["timezone"] in {"Europe/Rome", "Europe/London", "UTC", "America/New_York", "Asia/Tokyo"}
    assert body["revision"] > 0 and body["etag"] == f'"{body["revision"]}"'


def assert_room(body: dict) -> None:
    assert set(body) == {"id", "hub_id", "name", "floor", "revision", "etag"}
    assert 1 <= len(body["name"]) <= 60 and -5 <= body["floor"] <= 200
    assert body["revision"] > 0 and body["etag"] == f'"{body["revision"]}"'


def assert_device(body: dict) -> None:
    assert set(body) == {"id", "hub_id", "room_id", "label", "address", "type", "revision", "etag"}
    assert 1 <= len(body["label"]) <= 80
    assert re.fullmatch(r"[A-F0-9]{12}", body["address"])
    assert body["type"] in {"SENSOR", "LIGHT", "SWITCH", "THERMOSTAT", "LOCK"}
    assert body["revision"] > 0 and body["etag"] == f'"{body["revision"]}"'


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=20.0, follow_redirects=False) as value:
        yield value


@pytest.fixture(scope="session")
def admin_headers(): return bearer(ADMIN_TOKEN)


@pytest.fixture(scope="session")
def user_headers(): return bearer(USER_TOKEN)


@pytest.fixture(scope="session")
def other_user_headers(): return bearer(SECOND_USER_TOKEN)


@pytest.fixture
def values(request):
    raw = re.sub(r"[^A-Za-z0-9]", "", request.node.name).upper() or "CASE"
    def serial(n: int) -> str: return f"H-{raw[:12]}-{raw[-12:]}-{n:02d}"[:32]
    def label(n: int) -> str: return f"Device {raw[:55]} {n}"[:80]
    def name(kind: str, n: int) -> str: return f"{kind} {raw[:50]} {n}"[:80]
    def address(n: int) -> str: return f"A{n:011X}"[-12:]
    return {"serial": serial, "label": label, "name": name, "address": address}


@pytest.fixture
def hub_factory(client, admin_headers, values):
    index = 0
    def create(*, owner_id=USER_ONE_ID, serial=None, name=None, timezone="UTC"):
        nonlocal index
        index += 1
        payload = {"owner_id": owner_id, "serial": serial or values["serial"](index), "name": name or values["name"]("Hub", index), "timezone": timezone}
        response = client.post("/hubs", headers=admin_headers, json=payload)
        body = assert_json(response, 201)
        assert_hub(body); assert_location(response, f"/hubs/{body['id']}"); assert_etag(response, body)
        assert body["revision"] == 1 and body["owner_id"] == owner_id
        assert assert_json(client.get(f"/hubs/{body['id']}", headers=admin_headers), 200) == body
        return body
    return create


@pytest.fixture
def room_factory(client, admin_headers, values):
    index = 0
    def create(hub: dict, *, name=None, floor=0):
        nonlocal index
        index += 1
        payload = {"hub_id": hub["id"], "name": name or values["name"]("Room", index)[:60], "floor": floor}
        response = client.post("/rooms", headers=admin_headers, json=payload)
        body = assert_json(response, 201)
        assert_room(body); assert_location(response, f"/rooms/{body['id']}"); assert_etag(response, body)
        assert body["revision"] == 1 and body["hub_id"] == hub["id"]
        return body
    return create


@pytest.fixture
def device_factory(client, admin_headers, values):
    index = 0
    def create(hub: dict, *, room=None, label=None, address=None, device_type="SWITCH"):
        nonlocal index
        index += 1
        payload = {"hub_id": hub["id"], "room_id": None if room is None else room["id"], "label": label or values["label"](index), "address": address or values["address"](index), "type": device_type}
        response = client.post("/devices", headers=admin_headers, json=payload)
        body = assert_json(response, 201)
        assert_device(body); assert_location(response, f"/devices/{body['id']}"); assert_etag(response, body)
        assert body["revision"] == 1 and body["hub_id"] == hub["id"]
        return body
    return create
