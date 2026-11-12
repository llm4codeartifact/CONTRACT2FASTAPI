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


def test_REQ_CASE_012_013_driver_authorization_validation_and_boundary(client, admin_headers, driver):
    assert client.get("/drivers").status_code == 401
    valid = client.get("/drivers", headers=admin_headers, params={"limit": 1, "after_id": 1})
    assert valid.status_code == 200 and len(valid.json()["items"]) <= 1
    assert client.get("/drivers", headers=admin_headers, params={"limit": 0}).status_code == 422
    malformed = client.post("/drivers", headers={**admin_headers, "Content-Type": "application/json"}, content=b"{")
    assert malformed.status_code == 400
    unsupported = client.post("/drivers", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"x")
    assert unsupported.status_code == 415
    before = client.get("/drivers", headers=admin_headers, params={"employee_code_prefix": "E2E-TRIP-REQ-INVALID"}).json()["items"]
    invalid = client.post("/drivers", headers=admin_headers, json={"employee_code": "x", "full_name": "a"})
    assert invalid.status_code == 422
    after = client.get("/drivers", headers=admin_headers, params={"employee_code_prefix": "E2E-TRIP-REQ-INVALID"}).json()["items"]
    assert after == before
    assert driver["employee_code"] == "E2E-TRIP-REQ-015"


def test_REQ_CASE_014_trip_list_is_owner_scoped_and_paged(client, user_headers, planned_trip):
    assert client.get("/trips").status_code == 401
    listed = client.get("/trips", headers=user_headers, params={"limit": 100})
    assert listed.status_code == 200
    assert any(item["id"] == planned_trip["id"] for item in listed.json()["items"])
    assert client.get("/trips", headers=user_headers, params={"limit": 0}).status_code == 422


def test_REQ_CASE_014_015_trip_create_conflict_and_unchanged_state(client, user_headers, driver, vehicle_id, planned_trip):
    fetched = client.get(f"/trips/{planned_trip['id']}", headers=user_headers)
    assert fetched.status_code == 200 and fetched.json()["status"] == "PLANNED"
    overlap = {"driver_id": driver["id"], "vehicle_id": vehicle_id, "purpose": "overlap", "planned_start": "2031-01-10T10:30:00Z", "planned_end": "2031-01-10T11:30:00Z"}
    failed = client.post("/trips", headers=user_headers, json=overlap)
    assert failed.status_code == 409
    assert client.get(f"/trips/{planned_trip['id']}", headers=user_headers).json()["status"] == "PLANNED"
    assert client.post("/trips", headers={**user_headers, "Content-Type": "text/plain"}, content=b"x").status_code == 415
    assert client.post("/trips", headers=user_headers, json={**overlap, "planned_end": overlap["planned_start"]}).status_code == 400
    assert client.get("/trips/0", headers=user_headers).status_code == 404


def test_REQ_CASE_016_017_trip_read_patch_and_ownership(client, user_headers, admin_headers, planned_trip):
    assert client.get(f"/trips/{planned_trip['id']}").status_code == 401
    changed = client.patch(f"/trips/{planned_trip['id']}", headers=user_headers, json={"purpose": "REQ-017 amended purpose"})
    assert changed.status_code == 200 and changed.json()["purpose"] == "REQ-017 amended purpose"
    repeat = client.patch(f"/trips/{planned_trip['id']}", headers=user_headers, json={"purpose": "REQ-017 amended purpose"})
    assert repeat.status_code == 200 and repeat.json()["purpose"] == "REQ-017 amended purpose"
    invalid = client.patch(f"/trips/{planned_trip['id']}", headers=user_headers, json={"purpose": "x"})
    assert invalid.status_code == 422
    assert client.get(f"/trips/{planned_trip['id']}", headers=user_headers).json()["purpose"] == "REQ-017 amended purpose"
    assert client.patch("/trips/0", headers=admin_headers, json={"purpose": "valid purpose"}).status_code == 404


def test_REQ_CASE_010_trip_owner_isolation_returns_403(client, other_user_headers, planned_trip):
    response = client.get(f"/trips/{planned_trip['id']}", headers=other_user_headers)
    assert response.status_code == 403
    assert client.get(f"/trips/{planned_trip['id']}", headers=other_user_headers).status_code == 403


def test_REQ_CASE_018_019_020_transition_errors_leave_trip_unchanged(client, user_headers, planned_trip):
    bad_start = client.post(f"/trips/{planned_trip['id']}/start", headers=user_headers, json={"actual_start": "2031-01-10T09:59:59Z"})
    assert bad_start.status_code == 409
    assert client.get(f"/trips/{planned_trip['id']}", headers=user_headers).json()["status"] == "PLANNED"
    bad_complete = client.post(f"/trips/{planned_trip['id']}/complete", headers=user_headers, json={"actual_end": "2031-01-10T10:30:00Z"})
    assert bad_complete.status_code == 409
    cancelled = client.post(f"/trips/{planned_trip['id']}/cancel", headers=user_headers, json={"cancelled_at": "2031-01-10T10:00:00Z", "cancellation_reason": "deterministic cancellation"})
    assert cancelled.status_code == 200
    body = client.get(f"/trips/{planned_trip['id']}", headers=user_headers).json()
    assert body["status"] == "CANCELLED" and body["cancellation_reason"] == "deterministic cancellation"
    assert client.post(f"/trips/{planned_trip['id']}/cancel", headers=user_headers, json={"cancelled_at": "2031-01-10T10:00:00Z", "cancellation_reason": "again"}).status_code == 409


def test_REQ_CASE_021_022_conflict_queries_validation_and_404(client, admin_headers, driver, vehicle_id, planned_trip):
    params = {"planned_start": "2031-01-10T10:30:00Z", "planned_end": "2031-01-10T10:45:00Z", "limit": 1}
    driver_result = client.get(f"/drivers/{driver['id']}/trip-conflicts", headers=admin_headers, params=params)
    vehicle_result = client.get(f"/vehicles/{vehicle_id}/trip-conflicts", headers=admin_headers, params=params)
    assert driver_result.status_code == 200 and vehicle_result.status_code == 200
    assert all(item["id"] != planned_trip["id"] or item["status"] in {"PLANNED", "IN_PROGRESS"} for item in driver_result.json()["items"])
    assert client.get("/drivers/0/trip-conflicts", headers=admin_headers, params=params).status_code == 404
    assert client.get(f"/vehicles/{vehicle_id}/trip-conflicts", headers=admin_headers, params={**params, "planned_end": "bad"}).status_code == 422


def test_REQ_CASE_payload_over_10_mib_is_413(client, admin_headers):
    response = client.post("/drivers", headers={**admin_headers, "Content-Type": "application/json"}, content=b"{" + b"x" * (10 * 1024 * 1024 + 1) + b"}")
    assert response.status_code == 413
