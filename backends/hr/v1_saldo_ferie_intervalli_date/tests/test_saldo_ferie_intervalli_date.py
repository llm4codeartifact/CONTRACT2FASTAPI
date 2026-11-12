from conftest import assert_json, assert_status


def test_health_405_and_all_private_routes_require_authentication(client, admin_headers):
    before = assert_json(client.get("/employees", headers=admin_headers), 200)
    health = assert_json(client.get("/health"), 200)
    assert health == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert_status(wrong_method, 405)
    assert "GET" in wrong_method.headers["allow"]
    private_routes = [
        ("GET", "/employees"), ("POST", "/employees"), ("GET", "/employees/1"),
        ("PATCH", "/employees/1"), ("DELETE", "/employees/1"),
        ("GET", "/leave-balances"), ("POST", "/leave-balances"),
        ("GET", "/leave-balances/1"), ("PATCH", "/leave-balances/1"),
        ("DELETE", "/leave-balances/1"), ("GET", "/leave-balances/1/history"),
        ("GET", "/leave-requests"), ("POST", "/leave-requests"),
        ("GET", "/leave-requests/1"), ("PATCH", "/leave-requests/1"),
        ("DELETE", "/leave-requests/1"), ("POST", "/leave-requests/1/submit"),
        ("POST", "/leave-requests/1/cancel"),
        ("GET", "/employees/1/leave-calendar?date_from=2025-01-01&date_to=2025-12-31"),
    ]
    for method, path in private_routes:
        assert_status(client.request(method, path, json={}), 401)
    assert assert_json(client.get("/employees", headers=admin_headers), 200) == before


def test_employee_and_balance_crud_validation_authorization_and_history(client, admin_headers, user_headers, other_user_headers):
    assert_json(client.get("/employees/1", headers=user_headers), 200)
    assert_status(client.get("/employees/2", headers=user_headers), 403)
    assert_status(client.post("/employees", headers=user_headers, json={"employee_code": "DENIED-A", "full_name": "Denied Employee", "hire_date": "2026-01-15"}), 403)
    assert_status(client.post("/employees", headers=admin_headers, json={"employee_code": "bad", "full_name": "x", "hire_date": "2024-01-01"}), 422)
    created = assert_json(client.post("/employees", headers=admin_headers, json={"employee_code": "HR-A-TEST", "full_name": "HTTP Balance Tester", "hire_date": "2026-01-15"}), 201)
    assert client.get(created["id"] and f"/employees/{created['id']}", headers=admin_headers).status_code == 200
    assert_status(client.post("/employees", headers=admin_headers, json={"employee_code": "HR-A-TEST", "full_name": "Duplicate Tester", "hire_date": "2026-01-15"}), 409)
    patched = assert_json(client.patch(f"/employees/{created['id']}", headers=admin_headers, json={"full_name": "HTTP Balance Tester Updated"}), 200)
    assert assert_json(client.get(f"/employees/{created['id']}", headers=admin_headers), 200) == patched
    assert_status(client.patch(f"/employees/{created['id']}", headers=admin_headers, json={}), 422)

    assert_json(client.get("/leave-balances/1", headers=user_headers), 200)
    assert_status(client.get("/leave-balances/3", headers=user_headers), 403)
    balance = assert_json(client.post("/leave-balances", headers=admin_headers, json={"employee_id": created["id"], "leave_type": "VACATION", "year": 2026, "opening_days": 18.5}), 201)
    assert balance["remaining_days"] == 18.5
    assert_status(client.post("/leave-balances", headers=admin_headers, json={"employee_id": created["id"], "leave_type": "VACATION", "year": 2026, "opening_days": 18.5}), 409)
    adjusted = assert_json(client.patch(f"/leave-balances/{balance['id']}", headers=admin_headers, json={"opening_days": 20.0}), 200)
    assert adjusted["remaining_days"] == 20.0
    history = assert_json(client.get(f"/leave-balances/{balance['id']}/history", headers=admin_headers), 200)
    assert [item["change_type"] for item in history["items"]] == ["INITIAL", "ADJUSTMENT"]
    assert_status(client.patch(f"/leave-balances/{balance['id']}", headers=other_user_headers, json={"opening_days": 21}), 403)
    assert_status(client.delete(f"/leave-balances/{balance['id']}", headers=admin_headers), 204)
    assert_status(client.get(f"/leave-balances/{balance['id']}", headers=admin_headers), 404)
    assert_status(client.delete(f"/employees/{created['id']}", headers=admin_headers), 204)


def test_leave_request_lifecycle_ownership_conflicts_and_persistence(client, admin_headers, user_headers, other_user_headers):
    assert_status(client.post("/leave-requests", headers=other_user_headers, json={"employee_id": 1, "leave_type": "VACATION", "start_date": "2026-03-01", "end_date": "2026-03-02", "requested_days": 2}), 403)
    assert_status(client.post("/leave-requests", headers=user_headers, json={"employee_id": 1, "leave_type": "VACATION", "start_date": "2026-03-03", "end_date": "2026-03-01", "requested_days": 2}), 422)
    request = assert_json(client.post("/leave-requests", headers=user_headers, json={"employee_id": 1, "leave_type": "VACATION", "start_date": "2026-03-01", "end_date": "2026-03-02", "requested_days": 2}), 201)
    assert_status(client.get(f"/leave-requests/{request['id']}", headers=other_user_headers), 403)
    patched = assert_json(client.patch(f"/leave-requests/{request['id']}", headers=user_headers, json={"requested_days": 1.5}), 200)
    assert patched["status"] == "DRAFT"
    submitted = assert_json(client.post(f"/leave-requests/{request['id']}/submit", headers=user_headers, json={"submitted_at": "2026-02-01T10:00:00Z"}), 200)
    assert submitted["status"] == "SUBMITTED"
    assert assert_json(client.post(f"/leave-requests/{request['id']}/submit", headers=user_headers, json={"submitted_at": "2026-02-01T10:00:00Z"}), 200) == submitted
    overlap = assert_json(client.post("/leave-requests", headers=user_headers, json={"employee_id": 1, "leave_type": "VACATION", "start_date": "2026-03-02", "end_date": "2026-03-04", "requested_days": 2}), 201)
    assert_status(client.post(f"/leave-requests/{overlap['id']}/submit", headers=user_headers, json={"submitted_at": "2026-02-02T10:00:00Z"}), 409)
    calendar = assert_json(client.get("/employees/1/leave-calendar", headers=user_headers, params={"date_from": "2026-03-01", "date_to": "2026-03-31"}), 200)
    assert [item["id"] for item in calendar["items"]] == [request["id"]]
    assert_status(client.get("/employees/1/leave-calendar", headers=user_headers, params={"date_from": "invalid", "date_to": "2026-03-31"}), 422)
    assert_status(client.get("/leave-requests", headers=user_headers, params={"employee_id": 0}), 422)
    assert_status(client.get("/employees/0", headers=user_headers), 404)
    assert_status(client.delete(f"/leave-requests/{request['id']}", headers=user_headers), 409)
    cancelled = assert_json(client.post(f"/leave-requests/{request['id']}/cancel", headers=admin_headers, json={"cancelled_at": "2026-02-03T10:00:00Z"}), 200)
    assert cancelled["status"] == "CANCELLED"
    listed = assert_json(client.get("/leave-requests", headers=user_headers, params={"employee_id": 1, "limit": 100}), 200)
    assert request["id"] in [item["id"] for item in listed["items"]]
    assert_status(client.post("/employees", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}"), 415)
    assert_status(client.post("/employees", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"employee_code":'), 400)
