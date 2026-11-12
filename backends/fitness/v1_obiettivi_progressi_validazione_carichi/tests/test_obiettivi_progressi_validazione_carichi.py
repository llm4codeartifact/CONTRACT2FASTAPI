from .conftest import assert_json, assert_page


def test_health_authentication_and_openapi_error_contract(client, user_headers):
    before = assert_json(client.get("/fitness-goals", headers=user_headers), 200)
    assert_page(before)
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health", json={})
    assert wrong_method.status_code == 405 and "GET" in wrong_method.headers["allow"]
    private_routes = [
        ("GET", "/fitness-goals"), ("POST", "/fitness-goals"), ("GET", "/fitness-goals/1"),
        ("PATCH", "/fitness-goals/1"), ("DELETE", "/fitness-goals/1"),
        ("GET", "/fitness-goals/1/progress"), ("GET", "/progress-events"),
        ("POST", "/progress-events"), ("GET", "/progress-events/1"),
        ("PATCH", "/progress-events/1"), ("DELETE", "/progress-events/1"),
        ("POST", "/progress-events/1/validate"), ("GET", "/load-limits"),
        ("POST", "/load-limits"), ("GET", "/load-limits/1"),
        ("PATCH", "/load-limits/1"), ("DELETE", "/load-limits/1"),
        ("GET", "/load-limits/validate"), ("GET", "/personal-records"),
    ]
    for method, path in private_routes:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (method, path, response.text)
    wrong_validate_method = client.post("/load-limits/validate", headers=user_headers, json={})
    assert wrong_validate_method.status_code == 405 and "GET" in wrong_validate_method.headers["allow"]
    schema = assert_json(client.get("/openapi.json"), 200)
    assert {"200", "503"}.issubset(schema["paths"]["/health"]["get"]["responses"])
    assert set(schema["paths"]["/load-limits/validate"]) == {"get"}
    for path, path_item in schema["paths"].items():
        if path != "/health":
            for operation in path_item.values():
                if isinstance(operation, dict):
                    assert "503" not in operation["responses"]
    assert assert_json(client.get("/fitness-goals", headers=user_headers), 200) == before


def test_goal_progress_load_validation_and_records_persist(client, user_headers, admin_headers):
    assert_page(assert_json(client.get("/fitness-goals", headers=user_headers, params={"limit": 1}), 200))
    goal_response = client.post(
        "/fitness-goals", headers=user_headers,
        json={"title": "HTTP C Distance", "metric": "DISTANCE", "target_value": 10, "start_date": "2026-01-01T00:00:00Z", "end_date": "2026-12-31T00:00:00Z"},
    )
    goal = assert_json(goal_response, 201)
    assert goal_response.headers["location"].endswith(f"/fitness-goals/{goal['id']}")
    goal = assert_json(client.patch(f"/fitness-goals/{goal['id']}", headers=user_headers, json={"title": "HTTP C Distance Updated"}), 200)
    assert assert_json(client.get(f"/fitness-goals/{goal['id']}", headers=user_headers), 200) == goal
    event_response = client.post(
        "/progress-events", headers=user_headers,
        json={"goal_id": goal["id"], "value": 4, "event_date": "2026-02-01T00:00:00Z", "notes": "first"},
    )
    event = assert_json(event_response, 201)
    assert event_response.headers["location"].endswith(f"/progress-events/{event['id']}")
    assert_page(assert_json(client.get("/progress-events", headers=user_headers, params={"goal_id": goal["id"], "limit": 100}), 200))
    event = assert_json(client.patch(f"/progress-events/{event['id']}", headers=user_headers, json={"value": 5}), 200)
    assert assert_json(client.get(f"/progress-events/{event['id']}", headers=user_headers), 200) == event
    progress = assert_json(client.get(f"/fitness-goals/{goal['id']}/progress", headers=user_headers), 200)
    assert progress["current_value"] == 5 and progress["total_events"] == 1
    records = assert_json(client.get("/personal-records", headers=user_headers, params={"metric": "DISTANCE"}), 200)
    assert any(item["progress_event_id"] == event["id"] for item in records["items"])

    limit_response = client.post("/load-limits", headers=admin_headers, json={"exercise": "http_c_lift", "experience_level": "BEGINNER", "max_kg": 50})
    load_limit = assert_json(limit_response, 201)
    assert limit_response.headers["location"].endswith(f"/load-limits/{load_limit['id']}")
    assert_page(assert_json(client.get("/load-limits", headers=user_headers, params={"limit": 100}), 200))
    load_limit = assert_json(client.patch(f"/load-limits/{load_limit['id']}", headers=admin_headers, json={"max_kg": 55}), 200)
    assert assert_json(client.get(f"/load-limits/{load_limit['id']}", headers=user_headers), 200) == load_limit
    validation = assert_json(client.get("/load-limits/validate", headers=user_headers, params={"exercise": "http_c_lift", "experience_level": "BEGINNER", "load_kg": 56}), 200)
    assert validation["exceeds_limit"] is True and validation["requires_admin_validation"] is True

    pending = assert_json(client.post("/progress-events", headers=user_headers, json={"goal_id": 1, "value": 61, "event_date": "2025-03-01T00:00:00Z", "allow_pending": True}), 201)
    assert pending["is_validated"] is False
    validated = assert_json(client.post(f"/progress-events/{pending['id']}/validate", headers=admin_headers, json={}), 200)
    assert validated["is_validated"] is True and validated["validated_by"] == 3
    assert client.delete(f"/load-limits/{load_limit['id']}", headers=admin_headers).status_code == 204
    assert client.delete(f"/progress-events/{event['id']}", headers=user_headers).status_code == 204
    assert client.delete(f"/fitness-goals/{goal['id']}", headers=user_headers).status_code == 204


def test_ownership_validation_conflicts_and_failed_writes_are_atomic(client, user_headers, other_user_headers, admin_headers):
    original_goal = assert_json(client.get("/fitness-goals/1", headers=user_headers), 200)
    assert client.get("/fitness-goals/1", headers=other_user_headers).status_code == 403
    assert client.patch("/fitness-goals/1", headers=other_user_headers, json={"title": "Denied"}).status_code == 403
    assert assert_json(client.get("/fitness-goals/1", headers=user_headers), 200) == original_goal
    assert client.post("/load-limits", headers=user_headers, json={"exercise": "no", "experience_level": "BEGINNER", "max_kg": 1}).status_code == 403
    assert client.post("/progress-events/1/validate", headers=user_headers, json={}).status_code == 403
    assert client.get("/progress-events/1", headers=other_user_headers).status_code == 403
    before_events = assert_json(client.get("/progress-events", headers=user_headers, params={"goal_id": 1, "limit": 100}), 200)
    assert client.post("/progress-events", headers=user_headers, json={"goal_id": 1, "value": 61, "event_date": "2025-03-02T00:00:00Z"}).status_code == 409
    assert assert_json(client.get("/progress-events", headers=user_headers, params={"goal_id": 1, "limit": 100}), 200) == before_events
    assert client.post("/fitness-goals", headers=user_headers, json={"title": "Bad dates", "metric": "WEIGHT", "target_value": 1, "start_date": "2026-12-31T00:00:00Z", "end_date": "2026-01-01T00:00:00Z"}).status_code == 400
    assert client.post("/load-limits", headers=admin_headers, json={"exercise": "bench_press", "experience_level": "BEGINNER", "max_kg": 60}).status_code == 409
    assert client.post("/fitness-goals", headers=user_headers, json={"title": "X", "metric": "WEIGHT", "target_value": 1, "start_date": "2026-01-01T00:00:00Z", "end_date": "2026-02-01T00:00:00Z"}).status_code == 422
    assert client.post("/fitness-goals", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post("/fitness-goals", headers={**user_headers, "Content-Type": "application/json"}, content=b'{"title":').status_code == 400
