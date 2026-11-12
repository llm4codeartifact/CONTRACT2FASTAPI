from conftest import error, ok


def reservation_payload(table_id, party_size, start_at, end_at, name="Ada Lovelace"):
    return {"table_id": table_id, "party_size": party_size, "guest_name": name, "start_at": start_at, "end_at": end_at}


def waitlist_payload(start_at, end_at, name="Grace Hopper", party_size=2):
    return {"guest_name": name, "party_size": party_size, "start_at": start_at, "end_at": end_at}


def test_health_authentication_and_table_crud(client, admin_headers, user_headers, table_code):
    assert ok(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    method_response = client.post("/health")
    assert method_response.status_code == 405 and "GET" in method_response.headers["allow"]
    error(client.get("/tables"), 401)
    error(client.post("/tables", headers=user_headers, json={"code": table_code, "seats": 2, "zone": "BAR"}), 403)
    error(client.post("/tables", content="{", headers={**admin_headers, "Content-Type": "application/json"}), 400)
    error(client.post("/tables", content="{}", headers={**admin_headers, "Content-Type": "text/plain"}), 415)
    error(client.post("/tables", headers=admin_headers, json={"code": "bad", "seats": 2, "zone": "BAR"}), 422)
    created_response = client.post("/tables", headers=admin_headers, json={"code": table_code, "seats": 2, "zone": "BAR"})
    created = ok(created_response, 201)
    table_id = created["id"]
    assert created_response.headers["location"].endswith(f"/tables/{table_id}")
    assert ok(client.get(f"/tables/{table_id}", headers=user_headers), 200) == created
    error(client.post("/tables", headers=admin_headers, json={"code": table_code, "seats": 2, "zone": "BAR"}), 409)
    assert ok(client.patch(f"/tables/{table_id}", headers=admin_headers, json={"seats": 3}), 200)["seats"] == 3
    assert client.delete(f"/tables/{table_id}", headers=admin_headers).status_code == 204
    error(client.get(f"/tables/{table_id}", headers=admin_headers), 404)


def test_reservation_cancellation_offers_lowest_waiting_entry_and_preserves_history(client, admin_headers, user_headers, other_user_headers):
    error(client.get("/reservations?from=2026-09-10T21:00:00Z&to=2026-09-10T20:00:00Z", headers=user_headers), 422)
    error(client.get("/reservations/1", headers=other_user_headers), 403)
    created = ok(client.post("/reservations", headers=user_headers, json=reservation_payload(3, 2, "2030-04-01T18:00:00Z", "2030-04-01T19:00:00Z")), 201)
    assert created["status"] == "BOOKED"
    error(client.post("/reservations", headers=user_headers, json=reservation_payload(3, 2, "2030-04-01T18:01:00Z", "2030-04-01T19:00:00Z")), 422)
    error(client.post("/reservations", headers=user_headers, json=reservation_payload(2, 3, "2030-04-01T18:00:00Z", "2030-04-01T19:00:00Z")), 409)
    cancelled = ok(client.post("/reservations/1/cancel", headers=user_headers, json={"cancelled_at": "2026-09-10T17:00:00Z", "cancellation_reason": "  Guest ill  ", "offer_expires_at": "2026-09-10T18:00:00Z"}), 200)
    assert cancelled["status"] == "CANCELLED" and cancelled["cancellation_reason"] == "Guest ill"
    persisted = ok(client.get("/reservations/1", headers=user_headers), 200)
    assert persisted["cancelled_at"] == cancelled["cancelled_at"] and persisted["cancelled_by_user_id"] == 1
    offered = ok(client.get("/waitlist/1", headers=other_user_headers), 200)
    assert offered["status"] == "OFFERED" and offered["held_table_id"] == 1 and offered["offer_expires_at"].startswith("2026-09-10T18:00:00")
    error(client.get("/waitlist/1", headers=user_headers), 403)
    accepted_response = client.post("/waitlist/1/accept", headers=other_user_headers, json={"accepted_at": "2026-09-10T17:30:00Z"})
    accepted = ok(accepted_response, 201)
    assert accepted_response.headers["location"].endswith(f"/reservations/{accepted['id']}")
    assert accepted["status"] == "BOOKED" and accepted["table_id"] == 1 and accepted["user_id"] == 2
    accepted_waitlist = ok(client.get("/waitlist/1", headers=other_user_headers), 200)
    assert accepted_waitlist["status"] == "ACCEPTED" and accepted_waitlist["accepted_at"].startswith("2026-09-10T17:30:00")
    error(client.post("/waitlist/1/accept", headers=other_user_headers, json={"accepted_at": "2026-09-10T17:35:00Z"}), 409)
    error(client.post("/reservations/1/cancel", headers=user_headers, json={"cancelled_at": "2026-09-10T17:05:00Z", "cancellation_reason": "Again", "offer_expires_at": "2026-09-10T18:30:00Z"}), 409)
    error(client.delete("/tables/1", headers=admin_headers), 409)


def test_waitlist_accept_expire_cancel_and_availability_are_persistent_and_read_only(client, admin_headers, user_headers, other_user_headers):
    waiting_response = client.post("/waitlist", headers=user_headers, json=waitlist_payload("2030-05-01T18:00:00Z", "2030-05-01T19:00:00Z", "  Barbara Liskov  "))
    waiting = ok(waiting_response, 201)
    waiting_id = waiting["id"]
    assert waiting_response.headers["location"].endswith(f"/waitlist/{waiting_id}") and waiting["status"] == "WAITING"
    error(client.post("/waitlist", headers=user_headers, json=waitlist_payload("2030-05-01T18:01:00Z", "2030-05-01T19:00:00Z")), 422)
    error(client.delete(f"/waitlist/{waiting_id}", headers=other_user_headers), 403)
    assert client.delete(f"/waitlist/{waiting_id}", headers=user_headers).status_code == 204
    assert ok(client.get(f"/waitlist/{waiting_id}", headers=user_headers), 200)["status"] == "CANCELLED"
    error(client.delete(f"/waitlist/{waiting_id}", headers=user_headers), 409)
    error(client.post("/waitlist/2/expire", headers=user_headers, json={"expired_at": "2026-09-10T18:00:00Z"}), 403)
    expired = ok(client.post("/waitlist/2/expire", headers=admin_headers, json={"expired_at": "2026-09-10T18:00:00Z"}), 200)
    assert expired["status"] == "EXPIRED" and expired["held_table_id"] is None
    error(client.post("/waitlist/2/expire", headers=admin_headers, json={"expired_at": "2026-09-10T18:05:00Z"}), 409)
    before = ok(client.get("/waitlist", headers=admin_headers), 200)
    availability = ok(client.get("/availability?start_at=2030-06-01T18:00:00Z&end_at=2030-06-01T19:00:00Z&party_size=2&zone=PRIVATE", headers=user_headers), 200)
    assert all(item["zone"] == "PRIVATE" and item["available_seats"] >= 2 for item in availability)
    after = ok(client.get("/waitlist", headers=admin_headers), 200)
    assert before == after
