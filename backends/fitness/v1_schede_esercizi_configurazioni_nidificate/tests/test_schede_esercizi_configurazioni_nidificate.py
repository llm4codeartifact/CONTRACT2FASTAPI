from .conftest import assert_json, assert_page


def test_health_authentication_and_openapi_error_contract(client, user_headers):
    before = assert_json(client.get("/workout-plans", headers=user_headers), 200)
    assert_page(before)
    health = assert_json(client.get("/health"), 200)
    assert health == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health", json={})
    assert wrong_method.status_code == 405 and "GET" in wrong_method.headers["allow"]
    private_routes = [
        ("GET", "/exercises"), ("POST", "/exercises"), ("GET", "/exercises/1"),
        ("PATCH", "/exercises/1"), ("DELETE", "/exercises/1"),
        ("GET", "/exercises/1/plan-usages"), ("GET", "/workout-plans"),
        ("POST", "/workout-plans"), ("GET", "/workout-plans/1"),
        ("PATCH", "/workout-plans/1"), ("DELETE", "/workout-plans/1"),
        ("POST", "/workout-plans/1/clone"), ("GET", "/workout-plans/1/configuration"),
        ("GET", "/plan-blocks"), ("POST", "/plan-blocks"), ("GET", "/plan-blocks/1"),
        ("PATCH", "/plan-blocks/1"), ("DELETE", "/plan-blocks/1"),
        ("POST", "/plan-blocks/1/move"),
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
    assert assert_json(client.get("/workout-plans", headers=user_headers), 200) == before


def test_exercises_and_plans_persist_with_nested_configuration(client, user_headers, admin_headers):
    exercises = assert_json(client.get("/exercises", headers=user_headers, params={"limit": 1}), 200)
    assert_page(exercises)
    created_exercise_response = client.post(
        "/exercises", headers=admin_headers,
        json={"code": "HTTP_A_LIFT", "name": "HTTP A Lift", "category": "CORE", "active": True},
    )
    exercise = assert_json(created_exercise_response, 201)
    assert created_exercise_response.headers["location"].endswith(f"/exercises/{exercise['id']}")
    assert assert_json(client.get(f"/exercises/{exercise['id']}", headers=user_headers), 200) == exercise
    exercise = assert_json(client.patch(f"/exercises/{exercise['id']}", headers=admin_headers, json={"name": "HTTP A Lift Updated"}), 200)
    assert exercise["name"] == "HTTP A Lift Updated"

    create_plan = client.post("/workout-plans", headers=user_headers, json={"name": "HTTP A Plan", "description": "nested flow"})
    plan = assert_json(create_plan, 201)
    assert create_plan.headers["location"].endswith(f"/workout-plans/{plan['id']}")
    assert plan["user_id"] == 1 and plan["status"] == "DRAFT"
    block_response = client.post(
        "/plan-blocks", headers=user_headers,
        json={"plan_id": plan["id"], "exercise_id": exercise["id"], "sets": [{"reps": 10, "rest_seconds": 60, "target_load_kg": 20}]},
    )
    block = assert_json(block_response, 201)
    assert block_response.headers["location"].endswith(f"/plan-blocks/{block['id']}")
    assert block["sets"][0]["reps"] == 10
    assert_page(assert_json(client.get("/plan-blocks", headers=user_headers, params={"plan_id": plan["id"], "limit": 100}), 200))
    assert assert_json(client.get(f"/plan-blocks/{block['id']}", headers=user_headers), 200) == block
    block = assert_json(client.patch(f"/plan-blocks/{block['id']}", headers=user_headers, json={"sets": [{"reps": 8, "rest_seconds": 90}]}), 200)
    assert block["sets"][0]["reps"] == 8
    assert assert_json(client.post(f"/plan-blocks/{block['id']}/move", headers=user_headers, json={"new_position": 1}), 200)["position"] == 1
    configuration = assert_json(client.get(f"/workout-plans/{plan['id']}/configuration", headers=user_headers), 200)
    assert configuration["plan_id"] == plan["id"] and configuration["blocks"][0]["sets"][0]["reps"] == 8
    usages = assert_json(client.get(f"/exercises/{exercise['id']}/plan-usages", headers=user_headers), 200)
    assert any(item["block_id"] == block["id"] for item in usages["items"])
    cloned = assert_json(client.post(f"/workout-plans/{plan['id']}/clone", headers=user_headers, json={"name": "HTTP A Copy"}), 201)
    assert cloned["status"] == "DRAFT"
    clone_configuration = assert_json(client.get(f"/workout-plans/{cloned['id']}/configuration", headers=user_headers), 200)
    assert clone_configuration["blocks"]
    updated_plan = assert_json(client.patch(f"/workout-plans/{plan['id']}", headers=user_headers, json={"description": "persisted"}), 200)
    assert assert_json(client.get(f"/workout-plans/{plan['id']}", headers=user_headers), 200) == updated_plan
    assert client.delete(f"/plan-blocks/{block['id']}", headers=user_headers).status_code == 204
    assert client.delete(f"/workout-plans/{plan['id']}", headers=user_headers).status_code == 204
    for cloned_block in clone_configuration["blocks"]:
        assert client.delete(f"/plan-blocks/{cloned_block['id']}", headers=user_headers).status_code == 204
    assert client.delete(f"/workout-plans/{cloned['id']}", headers=user_headers).status_code == 204
    assert client.delete(f"/exercises/{exercise['id']}", headers=admin_headers).status_code == 204


def test_authorization_validation_conflicts_and_failed_writes_are_atomic(client, user_headers, other_user_headers, admin_headers):
    original_plan = assert_json(client.get("/workout-plans/2", headers=user_headers), 200)
    assert client.get("/workout-plans/2", headers=other_user_headers).status_code == 403
    assert client.patch("/workout-plans/2", headers=other_user_headers, json={"name": "Denied"}).status_code == 403
    assert assert_json(client.get("/workout-plans/2", headers=user_headers), 200) == original_plan
    assert client.post("/exercises", headers=user_headers, json={"code": "NO", "name": "Denied", "category": "CORE"}).status_code == 403
    assert client.patch("/workout-plans/1", headers=user_headers, json={"name": "Immutable"}).status_code == 409
    assert client.post("/plan-blocks", headers=user_headers, json={"plan_id": 1, "exercise_id": 1, "sets": [{"reps": 1, "rest_seconds": 0}]}).status_code == 409
    assert client.get("/plan-blocks/3", headers=other_user_headers).status_code == 403
    before = assert_json(client.get("/exercises", headers=admin_headers, params={"limit": 100}), 200)
    assert client.post("/exercises", headers=admin_headers, json={"code": "BENCH_PRESS", "name": "Duplicate", "category": "CHEST"}).status_code == 409
    assert client.post("/exercises", headers=admin_headers, json={"code": "X", "name": "Bad", "category": "CHEST"}).status_code == 422
    assert client.post("/exercises", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post("/exercises", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"code":').status_code == 400
    assert assert_json(client.get("/exercises", headers=admin_headers, params={"limit": 100}), 200) == before
