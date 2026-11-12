from .conftest import assert_json, assert_page


def test_health_authentication_and_openapi_error_contract(client, user_headers):
    before = assert_json(client.get("/workout-sessions", headers=user_headers), 200)
    assert_page(before)
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health", json={})
    assert wrong_method.status_code == 405 and "GET" in wrong_method.headers["allow"]
    private_routes = [
        ("GET", "/exercises"), ("POST", "/exercises"), ("GET", "/exercises/1"),
        ("PATCH", "/exercises/1"), ("DELETE", "/exercises/1"), ("GET", "/exercises/1/one-rm"),
        ("GET", "/workout-sessions"), ("POST", "/workout-sessions"),
        ("GET", "/workout-sessions/1"), ("PATCH", "/workout-sessions/1"),
        ("DELETE", "/workout-sessions/1"), ("POST", "/workout-sessions/1/complete"),
        ("GET", "/workout-sessions/1/volume"), ("GET", "/performed-sets"),
        ("POST", "/performed-sets"), ("GET", "/performed-sets/1"),
        ("PATCH", "/performed-sets/1"), ("DELETE", "/performed-sets/1"),
        ("POST", "/performed-sets/1/void"),
    ]
    for method, path in private_routes:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (method, path, response.text)
    schema = assert_json(client.get("/openapi.json"), 200)
    assert {"200", "503"}.issubset(schema["paths"]["/health"]["get"]["responses"])
    for path, path_item in schema["paths"].items():
        if path != "/health":
            for operation in path_item.values():
                if isinstance(operation, dict):
                    assert "503" not in operation["responses"]
    assert assert_json(client.get("/workout-sessions", headers=user_headers), 200) == before


def test_exercise_session_set_volume_and_one_rm_lifecycle(client, user_headers, admin_headers):
    assert_page(assert_json(client.get("/exercises", headers=user_headers, params={"limit": 1}), 200))
    created_exercise_response = client.post("/exercises", headers=admin_headers, json={"code": "HTTP_B_LIFT", "name": "HTTP B Lift"})
    exercise = assert_json(created_exercise_response, 201)
    assert created_exercise_response.headers["location"].endswith(f"/exercises/{exercise['id']}")
    exercise = assert_json(client.patch(f"/exercises/{exercise['id']}", headers=admin_headers, json={"name": "HTTP B Lift Updated"}), 200)
    assert assert_json(client.get(f"/exercises/{exercise['id']}", headers=user_headers), 200) == exercise

    created_session_response = client.post("/workout-sessions", headers=user_headers, json={"started_at": "2026-01-01T10:00:00Z", "notes": "HTTP B flow"})
    session = assert_json(created_session_response, 201)
    assert created_session_response.headers["location"].endswith(f"/workout-sessions/{session['id']}")
    assert_page(assert_json(client.get("/workout-sessions", headers=user_headers, params={"limit": 100}), 200))
    session = assert_json(client.patch(f"/workout-sessions/{session['id']}", headers=user_headers, json={"notes": "persisted"}), 200)
    assert assert_json(client.get(f"/workout-sessions/{session['id']}", headers=user_headers), 200) == session
    set_response = client.post("/performed-sets", headers=user_headers, json={"session_id": session["id"], "exercise_id": exercise["id"], "reps": 5, "load_kg": 90, "rpe": 8})
    performed_set = assert_json(set_response, 201)
    assert set_response.headers["location"].endswith(f"/performed-sets/{performed_set['id']}")
    assert performed_set["calculated_1rm"] == 105.0
    assert_page(assert_json(client.get("/performed-sets", headers=user_headers, params={"session_id": session["id"], "limit": 100}), 200))
    performed_set = assert_json(client.patch(f"/performed-sets/{performed_set['id']}", headers=user_headers, json={"reps": 6}), 200)
    assert performed_set["calculated_1rm"] == 108.0
    volume = assert_json(client.get(f"/workout-sessions/{session['id']}/volume", headers=user_headers), 200)
    assert volume["total_volume_kg"] == 540.0 and volume["total_sets"] == 1
    one_rm = assert_json(client.get(f"/exercises/{exercise['id']}/one-rm", headers=user_headers), 200)
    assert one_rm["max_1rm"] == 108.0 and one_rm["based_on_set_id"] == performed_set["id"]
    voided = assert_json(client.post(f"/performed-sets/{performed_set['id']}/void", headers=user_headers, json={"reason": "HTTP test"}), 200)
    assert voided["is_void"] is True and assert_json(client.get(f"/performed-sets/{voided['id']}", headers=user_headers), 200)["is_void"] is True
    assert assert_json(client.get(f"/workout-sessions/{session['id']}/volume", headers=user_headers), 200)["total_sets"] == 0
    assert assert_json(client.post(f"/workout-sessions/{session['id']}/complete", headers=user_headers, json={}), 200)["status"] == "COMPLETED"

    disposable = assert_json(client.post("/workout-sessions", headers=user_headers, json={"started_at": "2026-01-02T10:00:00Z"}), 201)
    disposable_set = assert_json(client.post("/performed-sets", headers=user_headers, json={"session_id": disposable["id"], "exercise_id": exercise["id"], "reps": 1, "load_kg": 1}), 201)
    assert client.delete(f"/performed-sets/{disposable_set['id']}", headers=user_headers).status_code == 204
    assert client.delete(f"/workout-sessions/{disposable['id']}", headers=user_headers).status_code == 204
    assert client.delete(f"/exercises/{exercise['id']}", headers=admin_headers).status_code == 409
    removable = assert_json(client.post("/exercises", headers=admin_headers, json={"code": "HTTP_B_REMOVE", "name": "HTTP B Remove"}), 201)
    assert client.delete(f"/exercises/{removable['id']}", headers=admin_headers).status_code == 204


def test_ownership_validation_conflicts_and_failed_writes_are_atomic(client, user_headers, other_user_headers, admin_headers):
    original_session = assert_json(client.get("/workout-sessions/2", headers=user_headers), 200)
    assert client.get("/workout-sessions/2", headers=other_user_headers).status_code == 403
    assert client.patch("/workout-sessions/2", headers=other_user_headers, json={"notes": "Denied"}).status_code == 403
    assert assert_json(client.get("/workout-sessions/2", headers=user_headers), 200) == original_session
    assert client.post("/exercises", headers=user_headers, json={"code": "NO", "name": "Denied"}).status_code == 403
    assert client.patch("/workout-sessions/1", headers=user_headers, json={"notes": "immutable"}).status_code == 409
    assert client.post("/performed-sets", headers=user_headers, json={"session_id": 1, "exercise_id": 1, "reps": 1, "load_kg": 1}).status_code == 409
    assert client.get("/performed-sets/1", headers=other_user_headers).status_code == 403
    before = assert_json(client.get("/exercises", headers=admin_headers, params={"limit": 100}), 200)
    assert client.post("/exercises", headers=admin_headers, json={"code": "BENCH_PRESS", "name": "Duplicate"}).status_code == 409
    assert client.post("/performed-sets", headers=user_headers, json={"session_id": 2, "exercise_id": 1, "reps": 101, "load_kg": 1}).status_code == 422
    assert client.post("/exercises", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post("/exercises", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"code":').status_code == 400
    assert assert_json(client.get("/exercises", headers=admin_headers, params={"limit": 100}), 200) == before
