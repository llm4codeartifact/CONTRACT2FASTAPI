from conftest import assert_json


def test_health_method_and_all_private_routes_require_bearer_auth(client):
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    paths = client.get("/openapi.json").json()["paths"]
    assert "503" in paths["/health"]["get"]["responses"]
    assert all("503" not in operation["responses"] for path, item in paths.items() if path != "/health" for operation in item.values())
    response = client.post("/health")
    assert response.status_code == 405 and "GET" in response.headers["allow"]
    private_routes = [
        ("GET", "/properties"), ("POST", "/properties"), ("GET", "/properties/1"), ("PATCH", "/properties/1"), ("DELETE", "/properties/1"),
        ("GET", "/agents"), ("POST", "/agents"), ("GET", "/agents/1"), ("PATCH", "/agents/1"), ("DELETE", "/agents/1"),
        ("GET", "/visits"), ("POST", "/visits"), ("GET", "/visits/1"), ("PATCH", "/visits/1"), ("DELETE", "/visits/1"),
        ("POST", "/visits/1/check-in"), ("POST", "/visits/1/complete"),
        ("GET", "/agents/1/visit-conflicts"), ("GET", "/properties/1/visit-conflicts"),
    ]
    for method, path in private_routes:
        assert client.request(method, path, json={}).status_code == 401


def test_admin_managed_agents_and_properties_validate_and_persist(client, admin_headers, user_headers):
    assert_json(client.get("/properties", headers=admin_headers, params={"limit": 100}), 200)
    assert_json(client.get("/agents", headers=admin_headers, params={"limit": 100}), 200)
    assert client.get("/properties", headers=admin_headers, params={"after_id": 0}).status_code == 422
    assert client.post("/agents", headers=user_headers, json={"name": "Denied", "license_code": "DENY-01"}).status_code == 403
    assert client.post("/agents", headers=admin_headers, json={"name": "x", "license_code": "bad"}).status_code == 422
    agent_response = client.post("/agents", headers=admin_headers, json={"name": "HTTP Visit Agent", "license_code": "HTTP-VISIT-01"})
    agent = assert_json(agent_response, 201)
    assert agent_response.headers["location"].endswith(f"/agents/{agent['id']}")
    assert client.post("/agents", headers=admin_headers, json={"name": "Duplicate", "license_code": "HTTP-VISIT-01"}).status_code == 409
    assert client.patch(f"/agents/{agent['id']}", headers=user_headers, json={"name": "Denied"}).status_code == 403
    assert_json(client.patch(f"/agents/{agent['id']}", headers=admin_headers, json={"name": "HTTP Visit Agent Updated"}), 200)

    property_response = client.post("/properties", headers=admin_headers, json={"address": "HTTP Visit Property", "city": "Roma", "agent_id": agent["id"]})
    property_body = assert_json(property_response, 201)
    property_id = property_body["id"]
    assert property_response.headers["location"].endswith(f"/properties/{property_id}")
    assert client.post("/properties", headers=user_headers, json={"address": "Denied Property", "agent_id": agent["id"]}).status_code == 403
    assert client.patch(f"/properties/{property_id}", headers=user_headers, json={"city": "Milano"}).status_code == 403
    assert_json(client.patch(f"/properties/{property_id}", headers=admin_headers, json={"city": "Milano"}), 200)
    assert assert_json(client.get(f"/properties/{property_id}", headers=admin_headers), 200)["city"] == "Milano"
    assert client.post("/properties", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post("/properties", headers={**admin_headers, "Content-Type": "application/json"}, content=b"{").status_code == 400
    assert_json(client.patch(f"/properties/{property_id}", headers=admin_headers, json={"status": "ARCHIVED"}), 200)
    assert client.delete(f"/properties/{property_id}", headers=admin_headers).status_code == 204
    assert client.get(f"/properties/{property_id}", headers=admin_headers).status_code == 404
    assert client.delete(f"/agents/{agent['id']}", headers=admin_headers).status_code == 204


def test_visit_ownership_conflicts_transitions_and_reports(client, admin_headers, user_headers, other_user_headers):
    seed = assert_json(client.get("/visits/1", headers=user_headers), 200)
    assert seed["visitor_user_id"] == 1
    assert client.get("/visits/1", headers=other_user_headers).status_code == 200
    assert client.post(
        "/visits", headers=user_headers,
        json={"property_id": 1, "agent_id": 1, "visitor_name": "Conflict Visitor", "start_at": "2025-05-10T10:30:00Z", "end_at": "2025-05-10T11:30:00Z"},
    ).status_code == 409
    assert assert_json(client.get("/visits/1", headers=user_headers), 200)["status"] == "SCHEDULED"
    assert client.post("/visits", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post("/visits", headers=user_headers, json={"property_id": 1, "agent_id": 1, "visitor_name": "x", "start_at": "2031-01-01T10:00:00Z", "end_at": "2031-01-01T10:00:00Z"}).status_code == 422

    response = client.post(
        "/visits", headers=user_headers,
        json={"property_id": 1, "agent_id": 1, "visitor_name": "HTTP Visit Owner", "start_at": "2031-02-01T10:00:00Z", "end_at": "2031-02-01T11:00:00Z"},
    )
    visit = assert_json(response, 201)
    visit_id = visit["id"]
    assert response.headers["location"].endswith(f"/visits/{visit_id}")
    assert any(item["id"] == visit_id for item in assert_json(client.get("/visits", headers=user_headers, params={"limit": 100}), 200)["items"])
    assert client.patch(f"/visits/{visit_id}", headers=other_user_headers, json={"visitor_name": "Denied"}).status_code == 403
    assert_json(client.patch(f"/visits/{visit_id}", headers=user_headers, json={"visitor_name": "HTTP Visit Owner Updated"}), 200)
    checked_in = assert_json(client.post(f"/visits/{visit_id}/check-in", headers=user_headers, json={"checked_in_at": "2031-02-01T10:05:00Z"}), 200)
    assert checked_in["status"] == "CHECKED_IN"
    assert_json(client.post(f"/visits/{visit_id}/check-in", headers=user_headers, json={"checked_in_at": "2031-02-01T10:05:00Z"}), 200)
    completed = assert_json(client.post(f"/visits/{visit_id}/complete", headers=user_headers, json={"completed_at": "2031-02-01T10:45:00Z"}), 200)
    assert completed["status"] == "COMPLETED"
    assert client.delete(f"/visits/{visit_id}", headers=user_headers).status_code == 409

    cancellable = assert_json(client.post(
        "/visits", headers=user_headers,
        json={"property_id": 1, "agent_id": 1, "visitor_name": "HTTP Cancellable Visit", "start_at": "2031-02-02T10:00:00Z", "end_at": "2031-02-02T11:00:00Z"},
    ), 201)
    assert client.delete(f"/visits/{cancellable['id']}", headers=user_headers).status_code == 204
    assert assert_json(client.get(f"/visits/{cancellable['id']}", headers=user_headers), 200)["status"] == "CANCELLED"

    report_params = {"start_at": "2025-05-10T09:30:00Z", "end_at": "2025-05-10T10:30:00Z", "limit": 100}
    assert client.get("/agents/1/visit-conflicts", headers=user_headers, params=report_params).status_code == 403
    agent_report = assert_json(client.get("/agents/1/visit-conflicts", headers=admin_headers, params=report_params), 200)
    property_report = assert_json(client.get("/properties/1/visit-conflicts", headers=admin_headers, params=report_params), 200)
    assert [item["id"] for item in agent_report["items"]] == [1]
    assert [item["id"] for item in property_report["items"]] == [1]
    assert client.get("/properties/1/visit-conflicts", headers=admin_headers, params={"start_at": "bad", "end_at": "2025-05-10T10:30:00Z"}).status_code == 422
