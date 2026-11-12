def test_REQ_CASE_011_health_and_method_contract(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert {"status", "database"}.issubset(response.json())
    paths = client.get("/openapi.json").json()["paths"]
    assert "503" in paths["/health"]["get"]["responses"]
    assert all("503" not in operation["responses"] for path, item in paths.items() if path != "/health" for operation in item.values())
    wrong_method = client.post("/health")
    assert wrong_method.status_code == 405
    assert "GET" in wrong_method.headers.get("Allow", "")


def test_REQ_CASE_012_vehicle_list_auth_boundary_and_invalid_limit(client, admin_headers):
    assert client.get("/vehicles").status_code == 401
    response = client.get("/vehicles", headers=admin_headers, params={"active": "true", "limit": 1})
    assert response.status_code == 200 and len(response.json()["items"]) <= 1
    assert client.get("/vehicles", headers=admin_headers, params={"limit": 101}).status_code == 422


def test_REQ_CASE_013_maintenance_list_is_owner_scoped_and_paged(client, user_headers, scheduled_maintenance):
    assert client.get("/maintenances").status_code == 401
    listed = client.get("/maintenances", headers=user_headers, params={"limit": 100})
    assert listed.status_code == 200
    assert any(item["id"] == scheduled_maintenance["id"] for item in listed.json()["items"])
    assert client.get("/maintenances", headers=user_headers, params={"limit": 0}).status_code == 422


def test_REQ_CASE_013_014_create_invalid_conflict_and_persistence(client, user_headers, available_vehicle, scheduled_maintenance):
    read = client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers)
    assert read.status_code == 200 and read.json()["status"] == "SCHEDULED"
    invalid = client.post("/maintenances", headers=user_headers, json={"vehicle_id": available_vehicle["id"], "type": "INSPECTION", "title": "x", "planned_start": "2032-02-10T12:00:00Z", "planned_end": "2032-02-10T12:00:00Z"})
    assert invalid.status_code == 422
    overlap = client.post("/maintenances", headers=user_headers, json={"vehicle_id": available_vehicle["id"], "type": "REPAIR", "title": "overlapping repair", "planned_start": "2032-02-10T10:30:00Z", "planned_end": "2032-02-10T11:30:00Z"})
    assert overlap.status_code == 409
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers).json()["status"] == "SCHEDULED"
    assert client.post("/maintenances", headers={**user_headers, "Content-Type": "text/plain"}, content=b"x").status_code == 415
    malformed = client.post("/maintenances", headers={**user_headers, "Content-Type": "application/json"}, content=b"{")
    assert malformed.status_code == 400


def test_REQ_CASE_015_016_read_patch_invalid_and_nonpositive(client, user_headers, scheduled_maintenance):
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}").status_code == 401
    changed = client.patch(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers, json={"title": "REQ-016 amended inspection"})
    assert changed.status_code == 200 and changed.json()["title"] == "REQ-016 amended inspection"
    repeat = client.patch(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers, json={"title": "REQ-016 amended inspection"})
    assert repeat.status_code == 200
    failed = client.patch(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers, json={"title": "x"})
    assert failed.status_code == 422
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers).json()["title"] == "REQ-016 amended inspection"
    assert client.get("/maintenances/0", headers=user_headers).status_code == 404


def test_REQ_CASE_008_maintenance_owner_isolation_returns_403(client, other_user_headers, scheduled_maintenance):
    response = client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=other_user_headers)
    assert response.status_code == 403
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=other_user_headers).status_code == 403


def test_REQ_CASE_017_start_precondition_keeps_record_unchanged(client, user_headers, scheduled_maintenance):
    response = client.post(f"/maintenances/{scheduled_maintenance['id']}/start", headers=user_headers, json={"actual_start": "2032-02-10T09:59:59Z"})
    assert response.status_code == 409
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers).json()["status"] == "SCHEDULED"
    complete = client.post(f"/maintenances/{scheduled_maintenance['id']}/complete", headers=user_headers, json={"actual_end": "2032-02-10T10:30:00Z", "completion_notes": "must not complete before start", "next_vehicle_state": "AVAILABLE"})
    assert complete.status_code == 409
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers).json()["status"] == "SCHEDULED"
    assert client.post(f"/maintenances/{scheduled_maintenance['id']}/start", headers={**user_headers, "Content-Type": "text/plain"}, content=b"x").status_code == 415


def test_REQ_CASE_018_019_cancel_creates_immutable_history(client, user_headers, scheduled_maintenance):
    cancelled = client.post(f"/maintenances/{scheduled_maintenance['id']}/cancel", headers=user_headers, json={"cancelled_at": "2032-02-10T10:00:00Z", "cancellation_reason": "deterministic cancellation"})
    assert cancelled.status_code == 200
    record = client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers)
    assert record.status_code == 200 and record.json()["status"] == "CANCELLED"
    history = client.get(f"/maintenances/{scheduled_maintenance['id']}/history", headers=user_headers)
    assert history.status_code == 200
    assert [event["new_status"] for event in history.json()["items"]][-1] == "CANCELLED"
    repeated = client.post(f"/maintenances/{scheduled_maintenance['id']}/cancel", headers=user_headers, json={"cancelled_at": "2032-02-10T10:00:01Z", "cancellation_reason": "second cancellation"})
    assert repeated.status_code == 409
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}/history", headers=user_headers).json() == history.json()


def test_REQ_CASE_020_021_022_history_availability_and_validation(client, admin_headers, available_vehicle, scheduled_maintenance):
    history = client.get(f"/maintenances/{scheduled_maintenance['id']}/history", headers=admin_headers, params={"limit": 1})
    assert history.status_code == 200 and len(history.json()["items"]) <= 1
    vehicle_history = client.get(f"/vehicles/{available_vehicle['id']}/maintenance-history", headers=admin_headers, params={"limit": 100})
    assert vehicle_history.status_code == 200
    assert any(item["id"] == scheduled_maintenance["id"] for item in vehicle_history.json()["items"])
    availability = client.get(f"/vehicles/{available_vehicle['id']}/maintenance-availability", headers=admin_headers, params={"planned_start": "2032-02-10T10:30:00Z", "planned_end": "2032-02-10T10:45:00Z"})
    assert availability.status_code == 200
    assert client.get("/vehicles/0/maintenance-history", headers=admin_headers).status_code == 404
    assert client.get(f"/vehicles/{available_vehicle['id']}/maintenance-availability", headers=admin_headers, params={"planned_start": "bad", "planned_end": "2032-02-10T10:45:00Z"}).status_code == 422


def test_REQ_CASE_payload_over_10_mib_is_413(client, user_headers):
    response = client.post("/maintenances", headers={**user_headers, "Content-Type": "application/json"}, content=b"{" + b"x" * (10 * 1024 * 1024 + 1) + b"}")
    assert response.status_code == 413
