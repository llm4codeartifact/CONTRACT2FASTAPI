from conftest import create_scheduled_maintenance, create_vehicle


def test_REQ_FLEET_STATE_011_health_and_method_contract(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}
    paths = client.get("/openapi.json").json()["paths"]
    assert "503" in paths["/health"]["get"]["responses"]
    assert all("503" not in operation["responses"] for path, item in paths.items() if path != "/health" for operation in item.values())
    wrong_method = client.post("/health")
    assert wrong_method.status_code == 405
    assert "GET" in wrong_method.headers.get("Allow", "")


def test_REQ_FLEET_STATE_012_013_vehicle_list_create_and_invalid_writes(client, admin_headers):
    assert client.get("/vehicles").status_code == 401
    listed = client.get("/vehicles", headers=admin_headers, params={"limit": 1})
    assert listed.status_code == 200
    assert len(listed.json()["items"]) <= 1
    assert client.get("/vehicles", headers=admin_headers, params={"limit": 0}).status_code == 422
    malformed = client.post("/vehicles", headers={**admin_headers, "Content-Type": "application/json"}, content=b"{")
    assert malformed.status_code == 400
    unsupported = client.post("/vehicles", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"x")
    assert unsupported.status_code == 415
    vehicle = create_vehicle(client, admin_headers, "TS-101")
    duplicate = client.post(
        "/vehicles",
        headers=admin_headers,
        json={"license_plate": vehicle["license_plate"], "manufacturer": "Test Manufacturer", "model": "Test Model", "year": 2030},
    )
    assert duplicate.status_code == 409
    persisted = client.get(f"/vehicles/{vehicle['id']}", headers=admin_headers)
    assert persisted.status_code == 200
    assert persisted.json()["current_state"] == "AVAILABLE"


def test_REQ_FLEET_STATE_014_015_016_vehicle_read_patch_and_delete(client, admin_headers):
    vehicle = create_vehicle(client, admin_headers, "TS-102")
    assert client.get(f"/vehicles/{vehicle['id']}").status_code == 401
    changed = client.patch(f"/vehicles/{vehicle['id']}", headers=admin_headers, json={"model": "Updated Model"})
    assert changed.status_code == 200
    assert changed.json()["model"] == "Updated Model"
    invalid = client.patch(f"/vehicles/{vehicle['id']}", headers=admin_headers, json={"model": None})
    assert invalid.status_code == 422
    assert client.get(f"/vehicles/{vehicle['id']}", headers=admin_headers).json()["model"] == "Updated Model"
    assert client.get("/vehicles/0", headers=admin_headers).status_code == 404
    assert client.delete(f"/vehicles/{vehicle['id']}", headers=admin_headers).status_code == 204
    assert client.get(f"/vehicles/{vehicle['id']}", headers=admin_headers).status_code == 404
    assert client.delete("/vehicles/2", headers=admin_headers).status_code == 409


def test_REQ_FLEET_STATE_017_018_019_state_read_history_and_transition(client, admin_headers, user_headers):
    vehicle = create_vehicle(client, admin_headers, "TS-103")
    assert client.get(f"/vehicles/{vehicle['id']}/state").status_code == 401
    state = client.get(f"/vehicles/{vehicle['id']}/state", headers=user_headers)
    assert state.status_code == 200
    assert state.json()["target_state"] == "AVAILABLE"
    history = client.get(f"/vehicles/{vehicle['id']}/state-history", headers=admin_headers)
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1
    transition = client.post(
        f"/vehicles/{vehicle['id']}/state-transitions",
        headers=admin_headers,
        json={"target_state": "OUT_OF_SERVICE", "effective_at": "2035-01-01T00:00:00Z", "reason": "Deterministic state test"},
    )
    assert transition.status_code == 201
    assert transition.headers["Location"].endswith(f"/vehicles/{vehicle['id']}/state")
    assert transition.json()["target_state"] == "OUT_OF_SERVICE"
    updated_state = client.get(f"/vehicles/{vehicle['id']}/state", headers=user_headers)
    assert updated_state.json()["target_state"] == "OUT_OF_SERVICE"
    assert client.get(f"/vehicles/{vehicle['id']}/state-history", headers=admin_headers, params={"limit": 0}).status_code == 422


def test_REQ_FLEET_STATE_020_021_022_023_maintenance_create_list_read_and_patch(client, admin_headers, user_headers, other_user_headers, scheduled_maintenance):
    assert client.get("/maintenances").status_code == 401
    listed = client.get("/maintenances", headers=user_headers, params={"limit": 100})
    assert listed.status_code == 200
    assert any(item["id"] == scheduled_maintenance["id"] for item in listed.json()["items"])
    read = client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers)
    assert read.status_code == 200
    assert read.json()["status"] == "SCHEDULED"
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=other_user_headers).status_code == 403
    changed = client.patch(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers, json={"title": "Updated maintenance title"})
    assert changed.status_code == 200
    assert changed.json()["title"] == "Updated maintenance title"
    invalid = client.patch(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers, json={"title": None})
    assert invalid.status_code == 422
    assert client.get(f"/maintenances/{scheduled_maintenance['id']}", headers=user_headers).json()["title"] == "Updated maintenance title"
    assert client.get("/maintenances/0", headers=user_headers).status_code == 404


def test_REQ_FLEET_STATE_024_025_026_lifecycle_start_complete_and_cancel(client, user_headers, lifecycle_maintenance):
    vehicle, maintenance = lifecycle_maintenance
    started = client.post(
        f"/maintenances/{maintenance['id']}/start",
        headers=user_headers,
        json={"actual_start": "2035-06-10T10:00:00Z"},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "IN_PROGRESS"
    completed = client.post(
        f"/maintenances/{maintenance['id']}/complete",
        headers=user_headers,
        json={"completed_at": "2035-06-10T10:30:00Z", "completion_notes": "Completed by test", "next_vehicle_state": "AVAILABLE"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"
    state = client.get(f"/vehicles/{vehicle['id']}/state", headers=user_headers)
    assert state.status_code == 200
    assert state.json()["target_state"] == "AVAILABLE"


def test_REQ_FLEET_STATE_026_cancel_preserves_state_after_rejected_repeat(client, admin_headers, user_headers, lifecycle_maintenance):
    vehicle, maintenance = lifecycle_maintenance
    started = client.post(
        f"/maintenances/{maintenance['id']}/start",
        headers=user_headers,
        json={"actual_start": "2035-06-10T10:00:00Z"},
    )
    assert started.status_code == 200
    cancelled = client.post(
        f"/maintenances/{maintenance['id']}/cancel",
        headers=user_headers,
        json={"cancelled_at": "2035-06-10T10:15:00Z", "cancellation_reason": "Cancellation test", "next_vehicle_state": "AVAILABLE"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    repeated = client.post(
        f"/maintenances/{maintenance['id']}/cancel",
        headers=user_headers,
        json={"cancelled_at": "2035-06-10T10:16:00Z", "cancellation_reason": "Repeated cancellation", "next_vehicle_state": "AVAILABLE"},
    )
    assert repeated.status_code == 409
    state = client.get(f"/vehicles/{vehicle['id']}/state", headers=user_headers)
    assert state.status_code == 200
    assert state.json()["target_state"] == "AVAILABLE"
    assert client.get(f"/vehicles/{vehicle['id']}/state-history", headers=admin_headers).status_code == 200


def test_REQ_FLEET_STATE_payload_validation_and_resource_control(client, admin_headers):
    oversized = client.post(
        "/vehicles",
        headers={**admin_headers, "Content-Type": "application/json"},
        content=b"{" + b"x" * (10 * 1024 * 1024 + 1) + b"}",
    )
    assert oversized.status_code == 413
