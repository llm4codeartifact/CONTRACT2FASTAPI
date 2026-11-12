from conftest import error, ok


def test_health_method_and_authentication(client):
    assert ok(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    response = client.post("/health")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    error(client.get("/tables"), 401)
    error(client.get("/tables", headers={"Authorization": "Bearer bad"}), 401)


def test_tables_admin_writes_validation_and_deletion(client, admin_headers, user_headers, table_code):
    error(client.post("/tables", headers=user_headers, json={"code": table_code, "zone": "BAR", "seats": 2}), 403)
    error(client.post("/tables", content="{}", headers={**admin_headers, "Content-Type": "text/plain"}), 415)
    error(client.post("/tables", headers=admin_headers, json={"code": "bad", "zone": "BAR", "seats": 2}), 422)
    created = ok(client.post("/tables", headers=admin_headers, json={"code": table_code, "zone": "BAR", "seats": 2}), 201)
    table_id = created["id"]
    assert created["active"] is True
    assert client.get(f"/tables/{table_id}", headers=admin_headers).headers.get("content-type", "").startswith("application/json")
    assert ok(client.get(f"/tables/{table_id}", headers=user_headers), 200) == created
    assert client.post("/tables", headers=admin_headers, json={"code": table_code, "zone": "BAR", "seats": 2}).status_code == 409
    patched = ok(client.patch(f"/tables/{table_id}", headers=admin_headers, json={"seats": 3}), 200)
    assert patched["seats"] == 3
    assert ok(client.get(f"/tables/{table_id}", headers=admin_headers), 200)["seats"] == 3
    assert client.delete(f"/tables/{table_id}", headers=admin_headers).status_code == 204
    error(client.get(f"/tables/{table_id}", headers=admin_headers), 404)


def test_service_shift_lifecycle_validation_and_pagination(client, admin_headers, user_headers):
    error(client.get("/service-shifts"), 401)
    shifts = ok(client.get("/service-shifts?limit=1", headers=user_headers), 200)
    assert len(shifts["items"]) == 1 and shifts["next_after_id"] == shifts["items"][0]["id"]
    error(client.get("/service-shifts?date_from=2030-01-02&date_to=2030-01-01", headers=user_headers), 422)
    payload = {"service_date": "2030-01-15", "service_kind": "BREAKFAST", "start_at": "2030-01-15T08:00:00Z", "end_at": "2030-01-15T10:00:00Z", "turn_count": 2}
    error(client.post("/service-shifts", headers=user_headers, json=payload), 403)
    created_response = client.post("/service-shifts", headers=admin_headers, json=payload)
    created = ok(created_response, 201)
    shift_id = created["id"]
    assert created_response.headers["location"].endswith(f"/service-shifts/{shift_id}")
    error(client.post("/service-shifts", headers=admin_headers, json=payload), 409)
    error(client.patch(f"/service-shifts/{shift_id}", headers=admin_headers, json={}), 422)
    patched = ok(client.patch(f"/service-shifts/{shift_id}", headers=admin_headers, json={"turn_count": 3}), 200)
    assert patched["turn_count"] == 3
    assert ok(client.get(f"/service-shifts/{shift_id}", headers=user_headers), 200)["turn_count"] == 3
    assert client.delete(f"/service-shifts/{shift_id}", headers=admin_headers).status_code == 204
    error(client.get(f"/service-shifts/{shift_id}", headers=admin_headers), 404)


def test_seating_ownership_conflicts_transitions_and_relation_protection(client, admin_headers, user_headers, other_user_headers):
    payload = {"table_id": 1, "shift_id": 1, "turn_number": 1, "party_size": 2, "guest_name": "  Ada Lovelace  "}
    error(client.post("/seatings", content="{", headers={**user_headers, "Content-Type": "application/json"}), 400)
    created_response = client.post("/seatings", headers=user_headers, json=payload)
    created = ok(created_response, 201)
    seating_id = created["id"]
    assert created_response.headers["location"].endswith(f"/seatings/{seating_id}")
    assert created["guest_name"] == "Ada Lovelace" and created["status"] == "BOOKED"
    error(client.get(f"/seatings/{seating_id}", headers=other_user_headers), 403)
    assert ok(client.get(f"/seatings/{seating_id}", headers=admin_headers), 200)["id"] == seating_id
    listed = ok(client.get("/seatings?shift_id=1", headers=user_headers), 200)
    assert [item["id"] for item in listed["items"]] == [seating_id]
    error(client.post("/seatings", headers=user_headers, json=payload), 409)
    error(client.post("/seatings", headers=user_headers, json={**payload, "turn_number": 3}), 409)
    error(client.patch(f"/seatings/{seating_id}", headers=other_user_headers, json={"guest_name": "Grace"}), 403)
    patched = ok(client.patch(f"/seatings/{seating_id}", headers=user_headers, json={"guest_name": "Grace Hopper"}), 200)
    assert patched["guest_name"] == "Grace Hopper"
    error(client.patch("/tables/1", headers=admin_headers, json={"active": False}), 409)
    error(client.delete("/tables/1", headers=admin_headers), 409)
    error(client.delete("/service-shifts/1", headers=admin_headers), 409)
    cancelled = ok(client.post(f"/seatings/{seating_id}/cancel", headers=user_headers, json={"cancelled_at": "2030-01-01T12:00:00Z"}), 200)
    assert cancelled["status"] == "CANCELLED" and cancelled["cancelled_at"].startswith("2030-01-01T12:00:00")
    assert ok(client.get(f"/seatings/{seating_id}", headers=user_headers), 200)["status"] == "CANCELLED"
    error(client.patch(f"/seatings/{seating_id}", headers=user_headers, json={"party_size": 1}), 409)
    error(client.post(f"/seatings/{seating_id}/cancel", headers=user_headers, json={"cancelled_at": "2030-01-01T12:05:00Z"}), 409)
