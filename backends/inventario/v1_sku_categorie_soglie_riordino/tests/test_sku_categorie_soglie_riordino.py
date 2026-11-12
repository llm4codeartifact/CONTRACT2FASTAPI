import os

import httpx


BASE_URL = os.environ["API_BASE_URL"].rstrip("/")
ADMIN = {"Authorization": f"Bearer {os.getenv('API_ADMIN_TOKEN', 'admin-token-secret-12345')}"}
USER = {"Authorization": f"Bearer {os.getenv('API_USER_TOKEN', 'user1-token-secret-12345')}"}


def check(response: httpx.Response, status_code: int) -> dict:
    assert response.status_code == status_code, response.text
    return response.json() if response.content else {}


def test_health_and_method_contract():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        assert check(client.get("/health"), 200) == {"status": "healthy"}
        assert check(client.request("GET", "/health", content=b"x" * (10 * 1024 * 1024 + 1)), 200) == {"status": "healthy"}
        check(client.post("/health"), 405)


def test_authentication_and_read_authorization():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        check(client.get("/categories"), 401)
        assert check(client.get("/categories", headers=USER), 200)["items"]
        check(client.post("/categories", headers=USER, json={"name": "User Cannot Create"}), 403)


def test_catalog_reorder_lifecycle_validation_conflicts_and_persistence():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        check(client.post("/categories", headers={**ADMIN, "Content-Type": "text/plain"}, content=b"{}"), 415)
        check(client.post("/categories", headers={**ADMIN, "Content-Type": "application/json"}, content=b"{"), 400)
        check(client.post("/categories", headers=ADMIN, json={"name": "x"}), 422)
        check(client.post("/categories", headers=ADMIN, json={"name": "Electronics"}), 409)
        check(client.get("/categories/999999", headers=ADMIN), 404)
        check(client.get("/categories?parent_id=0", headers=USER), 422)
        check(client.post("/products", headers=ADMIN, json={"sku": "BAD-REF", "name": "Invalid Reference", "category_id": 0}), 422)
        check(client.get("/categories/0", headers=ADMIN), 404)

        category = check(client.post("/categories", headers=ADMIN, json={"name": "HTTP Test Category C"}), 201)
        category_id = category["id"]
        assert check(client.get(f"/categories/{category_id}", headers=ADMIN), 200)["name"] == "HTTP Test Category C"
        check(client.patch(f"/categories/{category_id}", headers=ADMIN, json={"name": "HTTP Test Category C Updated"}), 200)

        product = check(
            client.post(
                "/products",
                headers=ADMIN,
                json={"sku": "HTTP-C-001", "name": "HTTP Test Product C", "category_id": category_id},
            ),
            201,
        )
        product_id = product["id"]
        assert check(client.get("/products", headers=USER), 200)["items"]
        assert check(client.get(f"/products/{product_id}", headers=ADMIN), 200)["sku"] == "HTTP-C-001"
        check(client.patch(f"/products/{product_id}", headers=ADMIN, json={"name": "HTTP Test Product C Updated"}), 200)
        stock = check(
            client.post("/stock-levels", headers=ADMIN, json={"product_id": product_id, "location_code": "TEST-C-01", "quantity": 3}),
            200,
        )
        assert stock["quantity"] == 3
        summary = check(client.get(f"/products/{product_id}/stock-summary", headers=USER), 200)
        assert summary["total_stock"] == 3
        assert check(client.post(f"/products/{product_id}/deactivate", headers=ADMIN, json={}), 200)["is_active"] is False

        rule = check(
            client.post(
                "/reorder-rules",
                headers=ADMIN,
                json={"product_id": 1, "location_code": "TEST-C-RULE", "reorder_point": 4, "target_level": 10},
            ),
            201,
        )
        rule_id = rule["id"]
        assert check(client.get("/reorder-rules", headers=USER), 200)["items"]
        assert check(client.get(f"/reorder-rules/{rule_id}", headers=ADMIN), 200)["location_code"] == "TEST-C-RULE"
        check(client.patch(f"/reorder-rules/{rule_id}", headers=ADMIN, json={"target_level": 12}), 200)
        assert check(client.post(f"/reorder-rules/{rule_id}/acknowledge", headers=ADMIN, json={}), 200)["is_acknowledged"] is True
        candidates = check(client.get("/reorder-candidates?location_code=TEST-C-RULE", headers=USER), 200)
        assert candidates["items"][0]["rule_id"] == rule_id
        check(client.patch("/products/1", headers=ADMIN, json={"is_active": False}), 409)
        check(client.delete(f"/reorder-rules/{rule_id}", headers=ADMIN), 204)
        check(client.delete(f"/products/{product_id}", headers=ADMIN), 204)
        check(client.delete(f"/categories/{category_id}", headers=ADMIN), 204)
