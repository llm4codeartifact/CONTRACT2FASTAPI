from conftest import assert_json, assert_status


def _items(client, path, headers, **params):
    body = assert_json(client.get(path, headers=headers, params=params), 200)
    assert {"items", "limit", "next_after_id"} == set(body)
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    return body


def _appointment(animal_id, room_id, start_at, end_at, reason="Black box appointment"):
    return {"animal_id": animal_id, "room_id": room_id, "reason": reason, "start_at": start_at, "end_at": end_at}


def test_health_method_and_private_routes_require_authentication(client, admin_headers):
    before = _items(client, "/rooms", admin_headers, limit=100)["items"]
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert_status(wrong_method, 405)
    assert "GET" in wrong_method.headers["allow"].upper()
    private_requests = [
        ("GET", "/rooms"), ("POST", "/rooms"), ("GET", "/rooms/1"), ("PATCH", "/rooms/1"), ("DELETE", "/rooms/1"),
        ("GET", "/animals"), ("POST", "/animals"), ("GET", "/animals/1"), ("GET", "/appointments"),
        ("POST", "/appointments"), ("GET", "/appointments/1"), ("PATCH", "/appointments/1"),
        ("POST", "/appointments/1/check-in"), ("POST", "/appointments/1/complete"), ("POST", "/appointments/1/cancel"),
        ("GET", "/rooms/1/appointment-conflicts?start_at=2030-01-01T10:00:00Z&end_at=2030-01-01T10:15:00Z"),
        ("GET", "/animals/1/appointment-conflicts?start_at=2030-01-01T10:00:00Z&end_at=2030-01-01T10:15:00Z"),
    ]
    for method, path in private_requests:
        assert_status(client.request(method, path, json={}), 401)
    assert _items(client, "/rooms", admin_headers, limit=100)["items"] == before


def test_room_and_animal_crud_authorization_and_body_contract(client, admin_headers, user_headers, other_user_headers):
    assert_status(client.get("/rooms", headers=user_headers, params={"include_deleted": True}), 403)
    assert_status(client.post("/rooms", headers=user_headers, json={"name": "Denied", "room_type": "CONSULTATION", "capacity": 1}), 403)
    created_response = client.post("/rooms", headers=admin_headers, json={"name": "Black Box Consultation", "room_type": "CONSULTATION", "capacity": 2})
    room = assert_json(created_response, 201)
    assert created_response.headers["location"].endswith(f"/rooms/{room['id']}")
    assert_status(client.post("/rooms", headers=admin_headers, json={"name": "Black Box Consultation", "room_type": "CONSULTATION", "capacity": 2}), 409)
    assert_status(client.patch(f"/rooms/{room['id']}", headers=admin_headers, json={}), 400)
    changed = assert_json(client.patch(f"/rooms/{room['id']}", headers=admin_headers, json={"capacity": 3}), 200)
    assert changed["capacity"] == 3
    assert_json(client.get(f"/rooms/{room['id']}", headers=user_headers), 200)
    assert_status(client.post("/rooms", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}"), 415)
    assert_status(client.post("/rooms", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"name":'), 400)
    assert_status(client.post("/rooms", headers=admin_headers, json={"name": "x", "room_type": "CONSULTATION", "capacity": 0}), 422)

    animal_response = client.post("/animals", headers=user_headers, json={"name": "Black Box Patient", "species": "DOG"})
    animal = assert_json(animal_response, 201)
    assert animal_response.headers["location"].endswith(f"/animals/{animal['id']}")
    assert_json(client.get(f"/animals/{animal['id']}", headers=user_headers), 200)
    assert_status(client.get(f"/animals/{animal['id']}", headers=other_user_headers), 403)
    assert_status(client.get("/animals", headers=user_headers, params={"client_account_id": 1}), 403)
    assert_status(client.post("/animals", headers=user_headers, json={"name": "Foreign", "species": "CAT", "client_account_id": 2}), 403)
    admin_page = _items(client, "/animals", admin_headers, client_account_id=1, limit=100)
    assert animal["id"] in [item["id"] for item in admin_page["items"]]
def test_appointment_conflicts_transitions_and_persistence(client, admin_headers, user_headers):
    room = assert_json(client.post("/rooms", headers=admin_headers, json={"name": "Black Box Schedule Room", "room_type": "CONSULTATION", "capacity": 2}), 201)
    animal = assert_json(client.post("/animals", headers=user_headers, json={"name": "Black Box Schedule Patient", "species": "DOG"}), 201)
    first_response = client.post("/appointments", headers=user_headers, json=_appointment(animal["id"], room["id"], "2030-01-10T10:00:00Z", "2030-01-10T11:00:00Z"))
    first = assert_json(first_response, 201)
    assert first_response.headers["location"].endswith(f"/appointments/{first['id']}")
    assert first["status"] == "SCHEDULED"
    before_conflict = assert_json(client.get(f"/appointments/{first['id']}", headers=user_headers), 200)
    assert_status(client.post("/appointments", headers=user_headers, json=_appointment(animal["id"], room["id"], "2030-01-10T10:30:00Z", "2030-01-10T11:30:00Z")), 409)
    assert assert_json(client.get(f"/appointments/{first['id']}", headers=user_headers), 200) == before_conflict
    assert_status(client.post("/appointments", headers=user_headers, json=_appointment(animal["id"], room["id"], "2030-01-10T10:01:00Z", "2030-01-10T10:30:00Z")), 422)
    assert_status(client.patch(f"/rooms/{room['id']}", headers=admin_headers, json={"active": False}), 409)
    room_conflicts = _items(client, f"/rooms/{room['id']}/appointment-conflicts", admin_headers, start_at="2030-01-10T10:30:00Z", end_at="2030-01-10T10:45:00Z", limit=100)
    assert [item["id"] for item in room_conflicts["items"]] == [first["id"]]
    endpoint_only = _items(client, f"/animals/{animal['id']}/appointment-conflicts", user_headers, start_at="2030-01-10T11:00:00Z", end_at="2030-01-10T11:15:00Z", limit=100)
    assert endpoint_only["items"] == []
    patched = assert_json(client.patch(f"/appointments/{first['id']}", headers=user_headers, json={"reason": "Black box patched appointment"}), 200)
    assert patched["reason"] == "Black box patched appointment"
    assert_status(client.patch(f"/appointments/{first['id']}", headers=user_headers, json={"end_at": "2030-01-10T10:00:00Z"}), 422)
    assert_status(client.post(f"/appointments/{first['id']}/check-in", headers=user_headers, json={"checked_in_at": "2030-01-10T09:55:00Z"}), 409)
    checked = assert_json(client.post(f"/appointments/{first['id']}/check-in", headers=user_headers, json={"checked_in_at": "2030-01-10T10:30:00Z"}), 200)
    assert checked["status"] == "CHECKED_IN"
    assert_status(client.post(f"/appointments/{first['id']}/complete", headers=user_headers, json={"completed_at": "2030-01-10T10:20:00Z"}), 409)
    completed = assert_json(client.post(f"/appointments/{first['id']}/complete", headers=user_headers, json={"completed_at": "2030-01-10T10:45:00Z"}), 200)
    assert completed["status"] == "COMPLETED" and completed["room_id"] == room["id"]
    assert_status(client.post(f"/appointments/{first['id']}/cancel", headers=user_headers, json={"cancelled_at": "2030-01-10T10:50:00Z", "cancellation_reason": "Too late"}), 409)

    cancellable = assert_json(client.post("/appointments", headers=user_headers, json=_appointment(animal["id"], room["id"], "2030-01-10T12:00:00Z", "2030-01-10T13:00:00Z")), 201)
    cancelled = assert_json(client.post(f"/appointments/{cancellable['id']}/cancel", headers=user_headers, json={"cancelled_at": "2030-01-10T12:05:00Z", "cancellation_reason": "Black box cancellation"}), 200)
    assert cancelled["status"] == "CANCELLED" and cancelled["cancelled_by_account_id"] == 1
    replacement = assert_json(client.post("/appointments", headers=user_headers, json=_appointment(animal["id"], room["id"], "2030-01-10T12:15:00Z", "2030-01-10T12:45:00Z")), 201)
    assert replacement["status"] == "SCHEDULED"
    assert_status(client.delete(f"/rooms/{room['id']}", headers=admin_headers), 409)
    assert_status(client.get("/appointments", headers=user_headers, params={"from": "2030-01-11T00:00:00Z", "to": "2030-01-10T00:00:00Z"}), 400)
    assert_status(client.get(f"/rooms/{room['id']}/appointment-conflicts", headers=user_headers, params={"start_at": "2030-01-10T12:15:00Z", "end_at": "2030-01-10T12:30:00Z"}), 403)


def test_unreferenced_room_soft_deletes_and_is_hidden(client, admin_headers):
    room = assert_json(client.post("/rooms", headers=admin_headers, json={"name": "Black Box Disposable Room", "room_type": "IMAGING", "capacity": 1}), 201)
    assert_status(client.delete(f"/rooms/{room['id']}", headers=admin_headers), 204)
    assert_status(client.get(f"/rooms/{room['id']}", headers=admin_headers), 404)
    assert_status(client.delete(f"/rooms/{room['id']}", headers=admin_headers), 404)
