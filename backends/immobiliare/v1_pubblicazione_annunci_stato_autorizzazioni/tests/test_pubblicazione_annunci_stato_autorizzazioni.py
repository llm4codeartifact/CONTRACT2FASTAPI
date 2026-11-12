from conftest import assert_json


def test_public_routes_health_method_and_private_authentication(client):
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    paths = client.get("/openapi.json").json()["paths"]
    assert "503" in paths["/health"]["get"]["responses"]
    assert all("503" not in operation["responses"] for path, item in paths.items() if path != "/health" for operation in item.values())
    response = client.post("/health")
    assert response.status_code == 405 and "GET" in response.headers["allow"]
    assert_json(client.get("/public/listings", params={"limit": 100}), 200)
    private_routes = [
        ("GET", "/properties"), ("POST", "/properties"), ("GET", "/properties/1"), ("PATCH", "/properties/1"), ("DELETE", "/properties/1"),
        ("GET", "/agents"), ("POST", "/agents"), ("GET", "/agents/1"), ("PATCH", "/agents/1"), ("DELETE", "/agents/1"),
        ("GET", "/listings"), ("POST", "/listings"), ("GET", "/listings/1"), ("PATCH", "/listings/1"), ("DELETE", "/listings/1"),
        ("POST", "/listings/1/publish"), ("POST", "/listings/1/unpublish"), ("GET", "/listings/1/history"),
    ]
    for method, path in private_routes:
        assert client.request(method, path, json={}).status_code == 401


def test_agents_and_properties_validate_ownership_and_persist(client, admin_headers, user_headers, other_user_headers):
    assert_json(client.get("/agents", headers=admin_headers, params={"limit": 100}), 200)
    assert client.get("/properties", headers=admin_headers, params={"after_id": 0}).status_code == 422
    assert client.post("/agents", headers=admin_headers, json={"name": "x", "email": "bad", "license_code": "bad"}).status_code == 422
    response = client.post("/agents", headers=admin_headers, json={"name": "HTTP Listing Agent", "email": "http-listing-agent@example.com", "license_code": "HTTP-LIST-01"})
    agent = assert_json(response, 201)
    assert response.headers["location"].endswith(f"/agents/{agent['id']}")
    assert client.post("/agents", headers=admin_headers, json={"name": "Duplicate", "email": "http-listing-agent@example.com", "license_code": "HTTP-LIST-02"}).status_code == 409
    assert client.patch(f"/agents/{agent['id']}", headers=other_user_headers, json={"name": "Denied"}).status_code == 403
    assert_json(client.patch(f"/agents/{agent['id']}", headers=admin_headers, json={"name": "HTTP Listing Agent Updated"}), 200)
    assert_json(client.get(f"/agents/{agent['id']}", headers=user_headers), 200)

    property_response = client.post(
        "/properties", headers=user_headers,
        json={"title": "HTTP Listing Property", "address": "Via HTTP 100, Milano", "price_cents": 75000000, "property_type": "APARTMENT"},
    )
    property_body = assert_json(property_response, 201)
    property_id = property_body["id"]
    assert property_response.headers["location"].endswith(f"/properties/{property_id}")
    assert client.post("/properties", headers=other_user_headers, json={"title": "Denied Property", "address": "Via Denied 1", "price_cents": 1000, "property_type": "HOUSE", "agent_id": 1}).status_code == 403
    assert client.post("/properties", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post("/properties", headers={**user_headers, "Content-Type": "application/json"}, content=b"{").status_code == 400
    assert client.patch(f"/properties/{property_id}", headers=other_user_headers, json={"title": "Denied Change"}).status_code == 403
    changed = assert_json(client.patch(f"/properties/{property_id}", headers=user_headers, json={"price_cents": 76000000}), 200)
    assert changed["price_cents"] == 76000000
    assert_json(client.get("/properties", headers=user_headers, params={"agent_id": 1, "limit": 100}), 200)
    assert client.delete(f"/properties/{property_id}", headers=user_headers).status_code == 409
    assert_json(client.patch(f"/properties/{property_id}", headers=user_headers, json={"status": "ARCHIVED"}), 200)
    assert client.delete(f"/properties/{property_id}", headers=user_headers).status_code == 204
    assert client.get(f"/properties/{property_id}", headers=user_headers).status_code == 404
    assert client.delete(f"/agents/{agent['id']}", headers=admin_headers).status_code == 204


def test_listing_lifecycle_public_projection_history_and_cleanup(client, admin_headers, user_headers, other_user_headers):
    property_id = 1
    listing_id = 1
    assert client.post("/listings", headers=other_user_headers, json={"property_id": property_id, "public_slug": "http-publication-disposable", "headline": "Denied listing"}).status_code == 403
    assert client.post("/listings", headers=user_headers, json={"property_id": property_id, "public_slug": "bad", "headline": "Bad slug"}).status_code == 422
    response = client.post(
        "/listings", headers=user_headers,
        json={"property_id": property_id, "public_slug": "http-publication-disposable", "headline": "HTTP publication headline", "description": "HTTP publication description"},
    )
    disposable = assert_json(response, 201)
    assert response.headers["location"].endswith(f"/listings/{disposable['id']}")
    assert client.post("/listings", headers=user_headers, json={"property_id": property_id, "public_slug": "http-publication-disposable", "headline": "Duplicate listing"}).status_code == 409
    assert client.post(f"/listings/{disposable['id']}/unpublish", headers=user_headers, json={"unpublished_at": "2031-03-02T10:00:00Z", "reason": "draft listing"}).status_code == 409
    assert client.delete(f"/listings/{disposable['id']}", headers=user_headers).status_code == 204
    assert client.patch(f"/listings/{listing_id}", headers=other_user_headers, json={"headline": "Denied update"}).status_code == 403
    assert_json(client.patch(f"/listings/{listing_id}", headers=user_headers, json={"headline": "HTTP publication amended"}), 200)
    assert any(item["id"] == listing_id for item in assert_json(client.get("/listings", headers=user_headers, params={"limit": 100}), 200)["items"])
    assert_json(client.get(f"/listings/{listing_id}", headers=user_headers), 200)
    assert_json(client.post(f"/listings/{listing_id}/unpublish", headers=user_headers, json={"unpublished_at": "2031-03-02T10:00:00Z", "reason": "HTTP test removal"}), 200)
    assert client.post(f"/listings/{listing_id}/publish", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    published = assert_json(client.post(f"/listings/{listing_id}/publish", headers=user_headers, json={"published_at": "2031-03-01T10:00:00Z"}), 200)
    assert published["state"] == "PUBLISHED"
    assert_json(client.post(f"/listings/{listing_id}/publish", headers=user_headers, json={"published_at": "2031-03-01T10:00:00Z"}), 200)
    public = assert_json(client.get("/public/listings", params={"property_type": "APARTMENT", "limit": 100}), 200)
    assert any(item["id"] == listing_id and item["property"]["id"] == property_id for item in public["items"])
    assert client.get(f"/listings/{listing_id}/history", headers=other_user_headers).status_code == 403
    history = assert_json(client.get(f"/listings/{listing_id}/history", headers=user_headers, params={"limit": 100}), 200)
    assert any(item["to_state"] == "PUBLISHED" for item in history["items"])
    assert client.delete(f"/listings/{listing_id}", headers=user_headers).status_code == 409
