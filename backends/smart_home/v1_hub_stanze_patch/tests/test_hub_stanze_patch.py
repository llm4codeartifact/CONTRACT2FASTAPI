import pytest

from .conftest import (
    USER_ONE_ID, USER_TWO_ID, assert_device, assert_error, assert_etag, assert_hub,
    assert_json, assert_room,
)


def _page(client, path, headers, **params):
    body = assert_json(client.get(path, headers=headers, params=params), 200)
    assert set(body) == {"items", "next_after_id"}
    assert [row["id"] for row in body["items"]] == sorted(row["id"] for row in body["items"])
    return body


def test_REQ_2_011_health_405_and_all_private_operations_require_bearer(client, admin_headers):
    before = {
        "hubs": _page(client, "/hubs", admin_headers, limit=100),
        "rooms": _page(client, "/rooms", admin_headers, limit=100),
        "devices": _page(client, "/devices", admin_headers, limit=100),
    }
    health = assert_json(client.get("/health"), 200)
    assert set(health) == {"status", "database"} and health["status"] == "ok"
    schema = assert_json(client.get("/openapi.json"), 200)
    assert set(schema["paths"]["/health"]["get"]["responses"]) == {"200", "503"}
    assert {"hubs": _page(client, "/hubs", admin_headers, limit=100), "rooms": _page(client, "/rooms", admin_headers, limit=100), "devices": _page(client, "/devices", admin_headers, limit=100)} == before
    method = client.post("/health")
    assert_error(method, 405); assert "GET" in method.headers["allow"].upper()

    private = [
        ("GET", "/hubs"), ("POST", "/hubs"), ("GET", "/hubs/0"), ("PATCH", "/hubs/0"), ("DELETE", "/hubs/0"),
        ("GET", "/rooms"), ("POST", "/rooms"), ("GET", "/rooms/0"), ("PATCH", "/rooms/0"), ("DELETE", "/rooms/0"),
        ("GET", "/devices"), ("POST", "/devices"), ("GET", "/devices/0"), ("PATCH", "/devices/0"), ("DELETE", "/devices/0"),
        ("GET", "/hubs/0/topology"),
    ]
    for method_name, path in private:
        assert_error(client.request(method_name, path, json={}), 401)


def test_REQ_2_001_005_006_007_hub_creation_listing_etag_patch_and_delete_are_atomic(
    client, admin_headers, user_headers, other_user_headers, hub_factory, room_factory, device_factory
):
    user_created_response = client.post("/hubs", headers=user_headers, json={"serial": "USER-HUB", "name": "U", "timezone": "Europe/Rome"})
    user_created = assert_json(user_created_response, 201)
    assert_hub(user_created); assert_etag(user_created_response, user_created)
    assert user_created["owner_id"] == USER_ONE_ID and user_created["revision"] == 1
    maximum = hub_factory(serial="H" * 32, name="N" * 80, timezone="Asia/Tokyo")
    invalid = client.post("/hubs", headers=admin_headers, json={"owner_id": USER_ONE_ID, "serial": "bad", "name": "Bad", "timezone": "UTC"})
    assert_error(invalid, 422)
    duplicate_before = assert_json(client.get(f"/hubs/{maximum['id']}", headers=admin_headers), 200)
    duplicate = client.post("/hubs", headers=admin_headers, json={"owner_id": USER_ONE_ID, "serial": maximum["serial"], "name": "Duplicate", "timezone": "UTC"})
    assert_error(duplicate, 409)
    assert assert_json(client.get(f"/hubs/{maximum['id']}", headers=admin_headers), 200) == duplicate_before

    second = hub_factory(timezone="Europe/Rome")
    page = _page(client, "/hubs", user_headers, owner_id=USER_ONE_ID, timezone="Europe/Rome", limit=1)
    assert page["items"] and page["next_after_id"] == page["items"][-1]["id"]
    following = _page(client, "/hubs", user_headers, owner_id=USER_ONE_ID, timezone="Europe/Rome", after_id=page["next_after_id"], limit=100)
    ids = [row["id"] for row in page["items"] + following["items"]]
    assert ids == sorted(ids) and len(ids) == len(set(ids)) and second["id"] in ids
    assert_error(client.get("/hubs", headers=user_headers, params={"owner_id": USER_TWO_ID}), 403)
    assert_error(client.get("/hubs", headers=admin_headers, params={"owner_id": 0}), 404)
    assert_error(client.get("/hubs", headers=admin_headers, params={"timezone": "Mars/Olympus"}), 422)
    assert_error(client.get("/hubs", headers=admin_headers, params={"after_id": 0}), 422)

    before = assert_json(client.get(f"/hubs/{user_created['id']}", headers=admin_headers), 200)
    assert_error(client.patch(f"/hubs/{user_created['id']}", headers=user_headers, json={"name": "Missing ETag"}), 422)
    assert_error(client.patch(f"/hubs/{user_created['id']}", headers={**user_headers, "If-Match": "1"}, json={"name": "Malformed ETag"}), 422)
    changed_response = client.patch(f"/hubs/{user_created['id']}", headers={**user_headers, "If-Match": before["etag"]}, json={"name": "Patched hub"})
    changed = assert_json(changed_response, 200)
    changed_etag = assert_etag(changed_response, changed)
    assert changed["revision"] == before["revision"] + 1 and changed["name"] == "Patched hub" and changed_etag != before["etag"]
    stale = client.patch(f"/hubs/{user_created['id']}", headers={**user_headers, "If-Match": before["etag"]}, json={"timezone": "UTC"})
    assert_error(stale, 409); assert stale.headers.get("etag") == changed_etag
    assert assert_json(client.get(f"/hubs/{user_created['id']}", headers=admin_headers), 200) == changed
    assert_error(client.patch(f"/hubs/{user_created['id']}", headers={**other_user_headers, "If-Match": changed_etag}, json={"name": "Foreign"}), 403)

    empty = hub_factory()
    deleted = client.delete(f"/hubs/{empty['id']}", headers=admin_headers)
    assert deleted.status_code == 204 and deleted.content == b""
    assert_error(client.get(f"/hubs/{empty['id']}", headers=admin_headers), 404)
    assert_error(client.delete(f"/hubs/{empty['id']}", headers=admin_headers), 404)
    occupied = hub_factory()
    room = room_factory(occupied)
    device = device_factory(occupied, room=room)
    occupied_before = assert_json(client.get(f"/hubs/{occupied['id']}", headers=admin_headers), 200)
    assert_error(client.delete(f"/hubs/{occupied['id']}", headers=admin_headers), 409)
    assert assert_json(client.get(f"/hubs/{occupied['id']}", headers=admin_headers), 200) == occupied_before
    assert assert_json(client.get(f"/rooms/{room['id']}", headers=admin_headers), 200) == room
    assert assert_json(client.get(f"/devices/{device['id']}", headers=admin_headers), 200) == device


def test_REQ_2_002_003_004_008_009_rooms_devices_topology_and_patch_relations(
    client, admin_headers, user_headers, other_user_headers, hub_factory, room_factory, device_factory
):
    hub = hub_factory()
    lower_room = room_factory(hub, name="A", floor=-5)
    upper_room = room_factory(hub, name="Z" * 60, floor=200)
    duplicate_room = client.post("/rooms", headers=user_headers, json={"hub_id": hub["id"], "name": "a", "floor": 0})
    assert_error(duplicate_room, 409)
    assert assert_json(client.get(f"/rooms/{lower_room['id']}", headers=admin_headers), 200) == lower_room
    assert_error(client.post("/rooms", headers=other_user_headers, json={"hub_id": hub["id"], "name": "No", "floor": 0}), 403)

    room_before = assert_json(client.get(f"/rooms/{lower_room['id']}", headers=admin_headers), 200)
    room_response = client.patch(f"/rooms/{lower_room['id']}", headers={**user_headers, "If-Match": room_before["etag"]}, json={"floor": 0})
    room_changed = assert_json(room_response, 200); assert_etag(room_response, room_changed)
    assert room_changed["revision"] == room_before["revision"] + 1 and room_changed["floor"] == 0
    stale_room = client.patch(f"/rooms/{lower_room['id']}", headers={**user_headers, "If-Match": room_before["etag"]}, json={"name": "Other"})
    assert_error(stale_room, 409); assert stale_room.headers.get("etag") == room_changed["etag"]
    assert_error(client.patch(f"/rooms/{lower_room['id']}", headers={**user_headers, "If-Match": room_changed["etag"]}, json={}), 422)
    assert assert_json(client.get(f"/rooms/{upper_room['id']}", headers=admin_headers), 200) == upper_room

    assigned = device_factory(hub, room=room_changed, label="L", address="A" * 12, device_type="LIGHT")
    unassigned = device_factory(hub, label="U" * 80, address="B" * 12, device_type="LOCK")
    assert_error(client.post("/devices", headers=user_headers, json={"hub_id": hub["id"], "room_id": room_changed["id"], "label": "L", "address": "C" * 12, "type": "SWITCH"}), 409)
    other_hub = hub_factory(owner_id=USER_TWO_ID)
    wrong_room = room_factory(other_hub)
    assigned_before = assert_json(client.get(f"/devices/{assigned['id']}", headers=admin_headers), 200)
    mismatched = client.patch(f"/devices/{assigned['id']}", headers={**user_headers, "If-Match": assigned_before["etag"]}, json={"room_id": wrong_room["id"]})
    assert_error(mismatched, 409)
    assert assert_json(client.get(f"/devices/{assigned['id']}", headers=admin_headers), 200) == assigned_before

    unassign_response = client.patch(f"/devices/{assigned['id']}", headers={**user_headers, "If-Match": assigned_before["etag"]}, json={"room_id": None})
    unassigned_device = assert_json(unassign_response, 200); assert_etag(unassign_response, unassigned_device)
    assert unassigned_device["room_id"] is None and unassigned_device["revision"] == assigned_before["revision"] + 1
    assert_error(client.patch(f"/devices/{assigned['id']}", headers={**user_headers, "If-Match": assigned_before["etag"]}, json={"label": "Stale"}), 409)
    assert_error(client.get(f"/devices/{assigned['id']}", headers=other_user_headers), 403)

    rooms = _page(client, "/rooms", user_headers, hub_id=hub["id"], limit=100)
    assert {lower_room["id"], upper_room["id"]}.issubset({row["id"] for row in rooms["items"]})
    devices = _page(client, "/devices", user_headers, hub_id=hub["id"], unassigned=True, limit=100)
    assert {unassigned["id"], unassigned_device["id"]}.issubset({row["id"] for row in devices["items"]})
    assert_error(client.get("/rooms", headers=admin_headers, params={"hub_id": 0}), 404)
    assert_error(client.get("/devices", headers=admin_headers, params={"room_id": 0}), 404)
    assert_error(client.get("/devices", headers=admin_headers, params={"type": "CAMERA"}), 422)
    assert_error(client.get("/rooms", headers=admin_headers, params={"after_id": 0}), 422)
    assert_error(client.get("/devices", headers=admin_headers, params={"after_id": 0}), 422)

    topology = assert_json(client.get(f"/hubs/{hub['id']}/topology", headers=user_headers), 200)
    assert set(topology) == {"hub", "rooms", "devices"}; assert_hub(topology["hub"])
    assert [row["id"] for row in topology["rooms"]] == sorted(row["id"] for row in topology["rooms"])
    assert [row["id"] for row in topology["devices"]] == sorted(row["id"] for row in topology["devices"])
    assert all(row["hub_id"] == hub["id"] for row in topology["rooms"] + topology["devices"])
    assert all(row["room_id"] in {None, lower_room["id"], upper_room["id"]} for row in topology["devices"])
    assert_error(client.get(f"/hubs/{hub['id']}/topology", headers=other_user_headers), 403)

    empty_room = room_factory(hub)
    assert client.delete(f"/rooms/{empty_room['id']}", headers=user_headers).status_code == 204
    occupied_room = room_factory(hub)
    occupied_device = device_factory(hub, room=occupied_room)
    assert_error(client.delete(f"/rooms/{occupied_room['id']}", headers=user_headers), 409)
    assert assert_json(client.get(f"/rooms/{occupied_room['id']}", headers=admin_headers), 200) == occupied_room
    hub_before_delete = assert_json(client.get(f"/hubs/{hub['id']}", headers=admin_headers), 200)
    room_before_delete = assert_json(client.get(f"/rooms/{occupied_room['id']}", headers=admin_headers), 200)
    assert client.delete(f"/devices/{occupied_device['id']}", headers=user_headers).status_code == 204
    assert assert_json(client.get(f"/hubs/{hub['id']}", headers=admin_headers), 200) == hub_before_delete
    assert assert_json(client.get(f"/rooms/{occupied_room['id']}", headers=admin_headers), 200) == room_before_delete
    assert_error(client.delete(f"/devices/{occupied_device['id']}", headers=user_headers), 404)


def test_REQ_2_write_media_malformed_and_oversize_requests_do_not_create_hub(client, admin_headers):
    unsupported = client.post("/hubs", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}")
    assert_error(unsupported, 415)
    malformed = client.post("/hubs", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"serial": "BROKEN"')
    assert_error(malformed, 400)
    oversized = b'{"serial":"OVERSIZE' + b"A" * (10 * 1024 * 1024) + b'"}'
    assert_error(client.post("/hubs", headers={**admin_headers, "Content-Type": "application/json"}, content=oversized), 413)
