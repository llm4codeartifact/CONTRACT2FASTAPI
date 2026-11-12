from conftest import assert_json, assert_status


def _items(client, path, headers, **params):
    body = assert_json(client.get(path, headers=headers, params=params), 200)
    assert {"items", "limit", "next_after_id"} == set(body)
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    return body


def test_health_method_and_all_private_routes_require_authentication(client, admin_headers):
    before = _items(client, "/animals", admin_headers, limit=100)["items"]
    health = assert_json(client.get("/health"), 200)
    assert health == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert_status(wrong_method, 405)
    assert "GET" in wrong_method.headers["allow"].upper()
    private_requests = [
        ("GET", "/owners", {}), ("POST", "/owners", {}), ("GET", "/owners/1", {}),
        ("PATCH", "/owners/1", {}), ("DELETE", "/owners/1", {}),
        ("GET", "/owners/1/animals", {}), ("GET", "/animals", {}),
        ("POST", "/animals", {}), ("GET", "/animals/1", {}),
        ("PATCH", "/animals/1", {}), ("DELETE", "/animals/1?ended_on=2030-01-01", {}),
        ("GET", "/ownerships", {}), ("GET", "/animals/1/ownerships", {}),
        ("POST", "/animals/1/ownerships", {}), ("GET", "/ownerships/1", {}),
        ("DELETE", "/ownerships/1?ended_on=2030-01-01", {}),
    ]
    for method, path, payload in private_requests:
        assert_status(client.request(method, path, json=payload), 401)
    assert _items(client, "/animals", admin_headers, limit=100)["items"] == before


def test_owner_validation_authorization_filters_and_unchanged_conflicts(client, admin_headers, user_headers, other_user_headers):
    assert_status(client.get("/owners", headers=user_headers), 403)
    owners = _items(client, "/owners", admin_headers, email_prefix="alice", limit=1)
    assert [item["email"] for item in owners["items"]] == ["alice@example.com"]
    assert_status(client.get("/owners", headers=admin_headers, params={"after_id": 0}), 422)
    assert_status(client.get("/animals", headers=admin_headers, params={"owner_id": 1, "unassigned": True}), 400)
    assert_status(client.get("/animals/1", headers=other_user_headers), 403)
    assert_status(client.get("/owners/1", headers=other_user_headers), 403)
    assert_json(client.get("/owners/1", headers=user_headers), 200)
    assert_status(client.post("/owners", headers=user_headers, json={"full_name": "User One", "email": "one@example.com", "account_id": 2}), 403)
    assert_status(client.post("/owners", headers=admin_headers, json={"full_name": "Nobody", "email": "nobody@example.com", "account_id": 99}), 404)
    assert_status(client.post("/owners", headers=admin_headers, json={"full_name": "Duplicate", "email": "alice@example.com", "account_id": 3}), 409)
    assert_status(client.post("/owners", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}"), 415)
    assert_status(client.post("/owners", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"full_name":'), 400)
    assert_status(client.post("/owners", headers=admin_headers, json={"full_name": "x", "email": "bad"}), 422)


def test_owner_animal_and_ownership_lifecycle_preserves_history(client, admin_headers, user_headers, other_user_headers):
    created_owner = client.post(
        "/owners", headers=admin_headers,
        json={"full_name": "Black Box Administrator", "email": "blackbox.admin@example.com", "account_id": 3},
    )
    owner = assert_json(created_owner, 201)
    assert created_owner.headers["location"].endswith(f"/owners/{owner['id']}")
    assert owner["account_id"] == 3 and owner["email"] == "blackbox.admin@example.com"
    patched_owner = assert_json(client.patch(f"/owners/{owner['id']}", headers=admin_headers, json={"phone": "+39 555 123456"}), 200)
    assert patched_owner["phone"] == "+39 555 123456"
    assert_status(client.patch(f"/owners/{owner['id']}", headers=admin_headers, json={}), 400)
    assert_status(client.patch(f"/owners/{owner['id']}", headers=other_user_headers, json={"full_name": "Denied"}), 403)

    owned_response = client.post(
        "/animals", headers=admin_headers,
        json={"name": "Black Box Owned", "species": "DOG", "sex": "MALE", "birth_date": "2024-01-01", "microchip": "111111111111111", "initial_owner_id": owner["id"], "ownership_started_on": "2030-01-01"},
    )
    owned = assert_json(owned_response, 201)
    assert owned_response.headers["location"].endswith(f"/animals/{owned['id']}")
    assert_status(client.get(f"/animals/{owned['id']}", headers=user_headers), 403)
    owner_animals = _items(client, f"/owners/{owner['id']}/animals", admin_headers, limit=100)
    assert [item["id"] for item in owner_animals["items"]] == [owned["id"]]
    changed = assert_json(client.patch(f"/animals/{owned['id']}", headers=admin_headers, json={"name": "Black Box Owned Patched"}), 200)
    assert changed["name"] == "Black Box Owned Patched"
    assert_status(client.patch(f"/animals/{owned['id']}", headers=admin_headers, json={"microchip": "123456789012345"}), 409)
    assert_json(client.get(f"/animals/{owned['id']}", headers=admin_headers), 200)
    assert_status(client.post(f"/animals/{owned['id']}/ownerships", headers=admin_headers, json={"owner_id": owner["id"], "started_on": "2030-02-01"}), 409)

    unowned = assert_json(client.post("/animals", headers=admin_headers, json={"name": "Black Box Transfer", "species": "CAT", "sex": "FEMALE", "microchip": "222222222222222"}), 201)
    relation_response = client.post(f"/animals/{unowned['id']}/ownerships", headers=admin_headers, json={"owner_id": owner["id"], "started_on": "2030-02-01"})
    relation = assert_json(relation_response, 201)
    assert relation_response.headers["location"].endswith(f"/ownerships/{relation['id']}")
    assert_json(client.get(f"/ownerships/{relation['id']}", headers=admin_headers), 200)
    assert_status(client.get(f"/ownerships/{relation['id']}", headers=user_headers), 403)
    assert_status(client.delete(f"/owners/{owner['id']}", headers=admin_headers), 409)
    assert_status(client.delete(f"/ownerships/{relation['id']}?ended_on=2030-01-31", headers=admin_headers), 409)
    assert_status(client.delete(f"/ownerships/{relation['id']}?ended_on=2030-02-02", headers=admin_headers), 204)
    ended_relation = assert_json(client.get(f"/ownerships/{relation['id']}", headers=admin_headers), 200)
    assert ended_relation["status"] == "ENDED" and ended_relation["ended_on"] == "2030-02-02"
    assert_status(client.delete(f"/ownerships/{relation['id']}?ended_on=2030-02-03", headers=admin_headers), 409)

    initial_relation = _items(client, "/ownerships", admin_headers, animal_id=owned["id"], status="ACTIVE", limit=100)["items"]
    assert len(initial_relation) == 1
    assert_status(client.delete(f"/ownerships/{initial_relation[0]['id']}?ended_on=2030-01-02", headers=admin_headers), 204)
    assert_status(client.delete(f"/owners/{owner['id']}", headers=admin_headers), 204)
    assert_status(client.get(f"/owners/{owner['id']}", headers=admin_headers), 404)
    deleted = _items(client, "/owners", admin_headers, include_deleted=True, limit=100)
    redacted = next(item for item in deleted["items"] if item["id"] == owner["id"])
    assert redacted["is_deleted"] and redacted["phone"] is None and redacted["email"].endswith("@deleted.local")
    assert_json(client.get(f"/animals/{owned['id']}", headers=admin_headers), 200)
    assert_status(client.delete(f"/animals/{owned['id']}?ended_on=2030-01-03", headers=admin_headers), 204)
    assert_status(client.get(f"/animals/{owned['id']}", headers=admin_headers), 404)
    historical = _items(client, "/ownerships", admin_headers, animal_id=owned["id"], limit=100)
    assert historical["items"][0]["status"] == "ENDED"
