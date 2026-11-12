from conftest import assert_json


def test_health_method_and_private_authentication(client):
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    paths = client.get("/openapi.json").json()["paths"]
    assert "503" in paths["/health"]["get"]["responses"]
    assert all("503" not in operation["responses"] for path, item in paths.items() if path != "/health" for operation in item.values())
    wrong_method = client.post("/health")
    assert wrong_method.status_code == 405 and "GET" in wrong_method.headers["allow"]

    private_routes = [
        ("GET", "/properties"), ("POST", "/properties"), ("GET", "/properties/1"),
        ("PATCH", "/properties/1"), ("DELETE", "/properties/1"), ("GET", "/agents"),
        ("POST", "/agents"), ("GET", "/agents/1"), ("PATCH", "/agents/1"),
        ("DELETE", "/agents/1"), ("GET", "/saved-searches"), ("POST", "/saved-searches"),
        ("GET", "/saved-searches/1"), ("PATCH", "/saved-searches/1"),
        ("DELETE", "/saved-searches/1"), ("GET", "/favorites"),
        ("POST", "/properties/1/favorite"), ("DELETE", "/properties/1/favorite"),
        ("GET", "/properties/1/similar"),
    ]
    for method, path in private_routes:
        assert client.request(method, path, json={}).status_code == 401
    assert client.get("/properties", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_agent_property_search_validation_ownership_and_persistence(client, admin_headers, user_headers, other_user_headers):
    assert client.get("/agents", headers=admin_headers, params={"after_id": 0}).status_code == 422
    assert client.post("/agents", headers=user_headers, json={"full_name": "No Admin", "email": "no-admin@example.com", "license_code": "NO-01"}).status_code == 403
    assert client.post("/agents", headers=admin_headers, json={"full_name": "x", "email": "bad", "license_code": "bad"}).status_code == 422
    agent_response = client.post(
        "/agents", headers=admin_headers,
        json={"full_name": "HTTP Search Agent", "email": "http-search-agent@example.com", "license_code": "HTTP-SEARCH-01"},
    )
    agent = assert_json(agent_response, 201)
    assert agent_response.headers["location"].endswith(f"/agents/{agent['id']}")
    assert_json(client.get(f"/agents/{agent['id']}", headers=user_headers), 200)
    assert client.post("/agents", headers=admin_headers, json={**agent, "full_name": "Duplicate", "active": True}).status_code == 422
    assert client.patch(f"/agents/{agent['id']}", headers=user_headers, json={"full_name": "Denied"}).status_code == 403
    assert assert_json(client.patch(f"/agents/{agent['id']}", headers=admin_headers, json={"full_name": "HTTP Search Agent Updated"}), 200)["full_name"] == "HTTP Search Agent Updated"

    property_response = client.post(
        "/properties", headers=user_headers,
        json={"title": "HTTP Search Property", "property_type": "APARTMENT", "city": "Milano", "area_sqm": 80, "rooms": 3, "price_cents": 55000000},
    )
    property_body = assert_json(property_response, 201)
    property_id = property_body["id"]
    assert property_response.headers["location"].endswith(f"/properties/{property_id}")
    listed = assert_json(client.get("/properties", headers=user_headers, params={"city": " milano ", "sort": "PRICE_ASC", "limit": 1}), 200)
    assert listed["limit"] == 1 and listed["items"][0]["city"] == "Milano"
    assert client.get("/properties", headers=user_headers, params={"price_min": 2, "price_max": 1}).status_code == 422
    assert client.get("/properties", headers=user_headers, params={"cursor": "not-a-cursor"}).status_code == 400
    assert client.patch(f"/properties/{property_id}", headers=other_user_headers, json={"city": "Roma"}).status_code == 403
    assert_json(client.patch(f"/properties/{property_id}", headers=user_headers, json={"city": "Roma"}), 200)
    assert assert_json(client.get(f"/properties/{property_id}", headers=user_headers), 200)["city"] == "Roma"
    assert client.post("/properties", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post("/properties", headers={**user_headers, "Content-Type": "application/json"}, content=b"{").status_code == 400
    assert_json(client.patch(f"/properties/{property_id}", headers=user_headers, json={"status": "ARCHIVED"}), 200)
    assert client.delete(f"/properties/{property_id}", headers=user_headers).status_code == 204
    assert client.get(f"/properties/{property_id}", headers=user_headers).status_code == 404
    assert client.delete(f"/agents/{agent['id']}", headers=admin_headers).status_code == 204


def test_saved_searches_favorites_and_similar_are_owned_and_persistent(client, admin_headers, user_headers, other_user_headers):
    seed_search = assert_json(client.get("/saved-searches/1", headers=user_headers), 200)
    assert seed_search["user_id"] == 1
    assert client.get("/saved-searches/1", headers=other_user_headers).status_code == 403
    created_response = client.post(
        "/saved-searches", headers=user_headers,
        json={"name": "HTTP Saved Search", "filters": {"city": "Milano", "property_type": "APARTMENT", "price_max": 90000000}},
    )
    search = assert_json(created_response, 201)
    assert created_response.headers["location"].endswith(f"/saved-searches/{search['id']}")
    assert client.post("/saved-searches", headers=user_headers, json={"name": "HTTP Saved Search", "filters": {}}).status_code == 409
    assert client.patch(f"/saved-searches/{search['id']}", headers=other_user_headers, json={"name": "Denied"}).status_code == 403
    changed = assert_json(client.patch(f"/saved-searches/{search['id']}", headers=user_headers, json={"name": "HTTP Saved Search Updated"}), 200)
    assert changed["name"] == "HTTP Saved Search Updated"
    assert any(item["id"] == search["id"] for item in assert_json(client.get("/saved-searches", headers=user_headers, params={"limit": 100}), 200)["items"])
    assert client.delete(f"/saved-searches/{search['id']}", headers=user_headers).status_code == 204
    assert client.get(f"/saved-searches/{search['id']}", headers=user_headers).status_code == 404

    favorite_response = client.post("/properties/1/favorite", headers=other_user_headers)
    favorite = assert_json(favorite_response, 201)
    assert favorite["property_id"] == 1 and favorite_response.headers["location"].endswith("/properties/1/favorite")
    assert client.post("/properties/1/favorite", headers=other_user_headers).status_code == 409
    assert any(item["id"] == favorite["id"] for item in assert_json(client.get("/favorites", headers=other_user_headers, params={"limit": 100}), 200)["items"])
    assert client.delete("/properties/1/favorite", headers=user_headers).status_code == 404
    assert client.delete("/properties/1/favorite", headers=other_user_headers).status_code == 204
    similar = assert_json(client.get("/properties/1/similar", headers=admin_headers, params={"limit": 100}), 200)
    assert all(item["id"] != 1 and item["city"] == "Milano" and item["property_type"] == "APARTMENT" for item in similar["items"])
