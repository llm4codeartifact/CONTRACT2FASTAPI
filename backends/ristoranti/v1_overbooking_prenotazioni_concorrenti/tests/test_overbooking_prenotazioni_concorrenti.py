from conftest import error, ok


def reservation_payload(table_id, party_size, start_at, end_at, name="Ada Lovelace"):
    return {"table_id": table_id, "party_size": party_size, "guest_name": name, "start_at": start_at, "end_at": end_at}


def test_health_method_authentication_and_table_lifecycle(client, admin_headers, user_headers, table_code):
    assert ok(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    method_response = client.post("/health")
    assert method_response.status_code == 405 and "GET" in method_response.headers["allow"]
    error(client.get("/tables"), 401)
    error(client.post("/tables", headers=user_headers, json={"code": table_code, "seats": 2, "overbook_seats": 0, "zone": "BAR"}), 403)
    error(client.post("/tables", content="{", headers={**admin_headers, "Content-Type": "application/json"}), 400)
    error(client.post("/tables", content="{}", headers={**admin_headers, "Content-Type": "text/plain"}), 415)
    error(client.post("/tables", headers=admin_headers, json={"code": "bad", "seats": 2, "overbook_seats": 0, "zone": "BAR"}), 422)
    created_response = client.post("/tables", headers=admin_headers, json={"code": table_code, "seats": 2, "overbook_seats": 1, "zone": "BAR"})
    created = ok(created_response, 201)
    table_id = created["id"]
    assert created_response.headers["location"].endswith(f"/tables/{table_id}")
    assert ok(client.get(f"/tables/{table_id}", headers=user_headers), 200) == created
    error(client.post("/tables", headers=admin_headers, json={"code": table_code, "seats": 2, "overbook_seats": 1, "zone": "BAR"}), 409)
    assert ok(client.patch(f"/tables/{table_id}", headers=admin_headers, json={"seats": 3}), 200)["seats"] == 3
    assert client.delete(f"/tables/{table_id}", headers=admin_headers).status_code == 204
    error(client.get(f"/tables/{table_id}", headers=admin_headers), 404)


def test_reservation_ownership_updates_and_terminal_history(client, admin_headers, user_headers, other_user_headers):
    payload = reservation_payload(1, 2, "2030-02-01T18:00:00Z", "2030-02-01T19:00:00Z", "  Ada Lovelace  ")
    error(client.post("/reservations", headers=user_headers, json={**payload, "start_at": "2030-02-01T18:01:00Z"}), 422)
    created_response = client.post("/reservations", headers=user_headers, json=payload)
    created = ok(created_response, 201)
    reservation_id = created["id"]
    assert created_response.headers["location"].endswith(f"/reservations/{reservation_id}")
    assert created["status"] == "PENDING" and created["guest_name"] == "Ada Lovelace"
    error(client.get(f"/reservations/{reservation_id}", headers=other_user_headers), 403)
    assert ok(client.get(f"/reservations/{reservation_id}", headers=admin_headers), 200)["id"] == reservation_id
    listed = ok(client.get("/reservations?table_id=1", headers=user_headers), 200)
    assert [item["id"] for item in listed["items"]] == [reservation_id]
    error(client.get("/reservations?from=2030-02-01T20:00:00Z&to=2030-02-01T19:00:00Z", headers=user_headers), 422)
    error(client.patch(f"/reservations/{reservation_id}", headers=other_user_headers, json={"guest_name": "Grace"}), 403)
    patched = ok(client.patch(f"/reservations/{reservation_id}", headers=user_headers, json={"guest_name": "Grace Hopper"}), 200)
    assert patched["guest_name"] == "Grace Hopper"
    confirmed = ok(client.post(f"/reservations/{reservation_id}/confirm", headers=user_headers, json={"confirmed_at": "2030-02-01T17:00:00Z"}), 200)
    assert confirmed["status"] == "CONFIRMED" and confirmed["confirmed_at"].startswith("2030-02-01T17:00:00")
    error(client.patch(f"/reservations/{reservation_id}", headers=user_headers, json={"party_size": 1}), 409)
    cancelled = ok(client.post(f"/reservations/{reservation_id}/cancel", headers=user_headers, json={"cancelled_at": "2030-02-01T17:05:00Z", "cancellation_reason": "  Guest ill  "}), 200)
    assert cancelled["status"] == "CANCELLED" and cancelled["cancellation_reason"] == "Guest ill"
    persisted = ok(client.get(f"/reservations/{reservation_id}", headers=user_headers), 200)
    assert persisted["cancelled_at"] == cancelled["cancelled_at"] and persisted["cancelled_by_user_id"] == 1
    error(client.post(f"/reservations/{reservation_id}/cancel", headers=user_headers, json={"cancelled_at": "2030-02-01T17:10:00Z", "cancellation_reason": "Again"}), 409)


def test_overbook_capacity_confirmation_no_show_availability_and_conflicts(client, admin_headers, user_headers):
    first = ok(client.post("/reservations", headers=user_headers, json=reservation_payload(2, 4, "2030-03-01T18:00:00Z", "2030-03-01T20:00:00Z", "First Guest")), 201)
    assert ok(client.post(f"/reservations/{first['id']}/confirm", headers=user_headers, json={"confirmed_at": "2030-03-01T17:00:00Z"}), 200)["status"] == "CONFIRMED"
    second = ok(client.post("/reservations", headers=user_headers, json=reservation_payload(2, 2, "2030-03-01T18:00:00Z", "2030-03-01T20:00:00Z", "Second Guest")), 201)
    error(client.post(f"/reservations/{second['id']}/confirm", headers=user_headers, json={"confirmed_at": "2030-03-01T17:05:00Z"}), 409)
    assert ok(client.get(f"/reservations/{second['id']}", headers=user_headers), 200)["status"] == "PENDING"
    error(client.post("/reservations", headers=user_headers, json=reservation_payload(2, 1, "2030-03-01T18:00:00Z", "2030-03-01T20:00:00Z", "Overflow")), 409)
    before = ok(client.get("/availability?table_id=2&start_at=2030-03-01T18:00:00Z&end_at=2030-03-01T20:00:00Z&party_size=1", headers=user_headers), 200)
    assert before["standard_remaining"] == 0 and before["overbook_remaining"] == 0 and before["can_book"] is False
    conflicts = ok(client.get("/tables/2/reservation-conflicts?start_at=2030-03-01T18:00:00Z&end_at=2030-03-01T20:00:00Z", headers=admin_headers), 200)
    assert {item["id"] for item in conflicts["items"]} == {first["id"], second["id"]}
    error(client.get("/tables/2/reservation-conflicts?start_at=2030-03-01T18:00:00Z&end_at=2030-03-01T20:00:00Z", headers=user_headers), 403)
    no_show = ok(client.post(f"/reservations/{second['id']}/no-show", headers=user_headers, json={"no_show_at": "2030-03-01T21:00:00Z"}), 200)
    assert no_show["status"] == "NO_SHOW" and no_show["no_show_by_user_id"] == 1
    after = ok(client.get("/availability?table_id=2&start_at=2030-03-01T18:00:00Z&end_at=2030-03-01T20:00:00Z&party_size=2", headers=user_headers), 200)
    assert after["overbook_remaining"] == 2 and after["can_book"] is True
    error(client.post(f"/reservations/{second['id']}/no-show", headers=user_headers, json={"no_show_at": "2030-03-01T21:05:00Z"}), 409)
