from conftest import assert_json, assert_status


def test_health_405_and_all_private_routes_require_authentication(client, admin_headers):
    before = assert_json(client.get("/employees", headers=admin_headers), 200)
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert_status(wrong_method, 405)
    assert "GET" in wrong_method.headers["allow"]
    private_routes = [
        ("GET", "/employees"), ("POST", "/employees"), ("GET", "/employees/1"),
        ("PATCH", "/employees/1"), ("DELETE", "/employees/1"),
        ("GET", "/leave-requests"), ("POST", "/leave-requests"),
        ("GET", "/leave-requests/1"), ("PATCH", "/leave-requests/1"),
        ("DELETE", "/leave-requests/1"), ("POST", "/leave-requests/1/approve"),
        ("POST", "/leave-requests/1/reject"), ("GET", "/leave-requests/1/history"),
        ("GET", "/approval-delegations"), ("POST", "/approval-delegations"),
        ("GET", "/approval-delegations/1"), ("PATCH", "/approval-delegations/1"),
        ("DELETE", "/approval-delegations/1"),
        ("GET", "/employees/1/leave-conflicts?start_date=2025-01-01&end_date=2025-12-31"),
    ]
    for method, path in private_routes:
        assert_status(client.request(method, path, json={}), 401)
    assert assert_json(client.get("/employees", headers=admin_headers), 200) == before


def test_employee_and_delegation_crud_validation_authorization_and_persistence(client, admin_headers, user_headers, manager_headers):
    assert_json(client.get("/employees/1", headers=user_headers), 200)
    assert_status(client.post("/employees", headers=user_headers, json={"employee_code": "DENIED-B", "full_name": "Denied Employee", "department": "Quality"}), 403)
    assert_status(client.post("/employees", headers=admin_headers, json={"employee_code": "bad", "full_name": "x", "department": "x"}), 422)
    employee = assert_json(client.post("/employees", headers=admin_headers, json={"employee_code": "HR-B-TEST", "full_name": "HTTP Approval Tester", "manager_employee_id": 3, "department": "Quality"}), 201)
    changed = assert_json(client.patch(f"/employees/{employee['id']}", headers=admin_headers, json={"department": "Quality Assurance"}), 200)
    assert assert_json(client.get(f"/employees/{employee['id']}", headers=admin_headers), 200) == changed
    assert_status(client.patch(f"/employees/{employee['id']}", headers=admin_headers, json={}), 422)
    assert_status(client.post("/employees", headers=admin_headers, json={"employee_code": "HR-B-TEST", "full_name": "Duplicate Tester", "department": "Quality"}), 409)

    assert_status(client.post("/approval-delegations", headers=user_headers, json={"manager_employee_id": 2, "delegate_employee_id": 1, "effective_from": "2026-01-01", "effective_to": "2026-01-31"}), 403)
    assert_status(client.post("/approval-delegations", headers=manager_headers, json={"manager_employee_id": 2, "delegate_employee_id": 2, "effective_from": "2026-01-01", "effective_to": "2026-01-31"}), 422)
    delegation = assert_json(client.post("/approval-delegations", headers=manager_headers, json={"manager_employee_id": 2, "delegate_employee_id": 1, "effective_from": "2026-01-01", "effective_to": "2026-01-31"}), 201)
    assert_json(client.get(f"/approval-delegations/{delegation['id']}", headers=user_headers), 200)
    assert_status(client.patch(f"/approval-delegations/{delegation['id']}", headers=user_headers, json={"effective_to": "2026-02-01"}), 403)
    patched = assert_json(client.patch(f"/approval-delegations/{delegation['id']}", headers=manager_headers, json={"effective_to": "2026-02-01"}), 200)
    listed = assert_json(client.get("/approval-delegations", headers=admin_headers, params={"manager_employee_id": 2, "limit": 100}), 200)
    assert patched["id"] in [item["id"] for item in listed["items"]]
    assert_status(client.post("/approval-delegations", headers=manager_headers, json={"manager_employee_id": 2, "delegate_employee_id": 3, "effective_from": "2026-01-15", "effective_to": "2026-02-15"}), 409)
    assert_status(client.delete(f"/approval-delegations/{delegation['id']}", headers=manager_headers), 204)
    assert_status(client.get(f"/approval-delegations/{delegation['id']}", headers=admin_headers), 404)
    assert_status(client.delete(f"/employees/{employee['id']}", headers=admin_headers), 204)


def test_request_approval_rejection_overlap_conflicts_history_and_visibility(client, admin_headers, user_headers, manager_headers):
    assert_status(client.post("/leave-requests", headers=manager_headers, json={"employee_id": 1, "leave_type": "VACATION", "start_date": "2026-04-01", "end_date": "2026-04-02", "requested_days": 2}), 403)
    assert_status(client.post("/leave-requests", headers=user_headers, json={"employee_id": 1, "leave_type": "VACATION", "start_date": "2026-04-02", "end_date": "2026-04-01", "requested_days": 2}), 422)
    approved_candidate = assert_json(client.post("/leave-requests", headers=user_headers, json={"employee_id": 1, "leave_type": "VACATION", "start_date": "2026-04-01", "end_date": "2026-04-02", "requested_days": 2}), 201)
    assert_status(client.post(f"/leave-requests/{approved_candidate['id']}/approve", headers=user_headers, json={"decided_at": "2026-03-01T10:00:00Z", "decision_reason": "Self approval"}), 403)
    approved = assert_json(client.post(f"/leave-requests/{approved_candidate['id']}/approve", headers=manager_headers, json={"decided_at": "2026-03-01T10:00:00Z", "decision_reason": "Coverage approved"}), 200)
    assert approved["status"] == "APPROVED" and approved["decider_employee_id"] == 2
    assert assert_json(client.post(f"/leave-requests/{approved_candidate['id']}/approve", headers=manager_headers, json={"decided_at": "2026-03-01T10:00:00Z", "decision_reason": "Coverage approved"}), 200) == approved
    history = assert_json(client.get(f"/leave-requests/{approved_candidate['id']}/history", headers=user_headers), 200)
    assert [item["action"] for item in history["items"]] == ["SUBMITTED", "APPROVED"]
    assert_status(client.patch(f"/leave-requests/{approved_candidate['id']}", headers=admin_headers, json={"requested_days": 1}), 409)
    assert_status(client.post("/leave-requests", headers=user_headers, json={"employee_id": 1, "leave_type": "SICK", "start_date": "2026-04-02", "end_date": "2026-04-03", "requested_days": 2}), 409)

    rejected_candidate = assert_json(client.post("/leave-requests", headers=user_headers, json={"employee_id": 1, "leave_type": "SICK", "start_date": "2026-05-01", "end_date": "2026-05-02", "requested_days": 2}), 201)
    rejected = assert_json(client.post(f"/leave-requests/{rejected_candidate['id']}/reject", headers=manager_headers, json={"decided_at": "2026-03-02T10:00:00Z", "decision_reason": "Insufficient coverage"}), 200)
    assert rejected["status"] == "REJECTED"
    conflicts = assert_json(client.get("/employees/1/leave-conflicts", headers=manager_headers, params={"start_date": "2026-04-01", "end_date": "2026-04-30"}), 200)
    assert [item["id"] for item in conflicts["items"]] == [approved_candidate["id"]]
    assert_status(client.get("/employees/1/leave-conflicts", headers=manager_headers, params={"start_date": "invalid", "end_date": "2026-04-30"}), 422)
    assert_status(client.get("/approval-delegations", headers=manager_headers, params={"manager_employee_id": 0}), 422)
    assert_status(client.get("/employees/0", headers=manager_headers), 404)
    requests = assert_json(client.get("/leave-requests", headers=manager_headers, params={"employee_id": 1, "limit": 100}), 200)
    assert approved_candidate["id"] in [item["id"] for item in requests["items"]]
    assert_status(client.post("/employees", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}"), 415)
