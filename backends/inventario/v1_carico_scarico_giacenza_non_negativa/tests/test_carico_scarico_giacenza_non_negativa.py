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
        health = check(client.get("/health"), 200)
        assert health == {"status": "ok", "database": "connected"}
        check(client.post("/health"), 405)


def test_authentication_and_read_authorization():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        check(client.get("/products"), 401)
        products = check(client.get("/products", headers=USER), 200)
        assert products["items"]
        check(client.post("/products", headers=USER, json={"name": "User Cannot Create"}), 403)


def test_inventory_lifecycle_validation_conflicts_and_persistence():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        check(client.post("/products", headers={**ADMIN, "Content-Type": "text/plain"}, content=b"{}"), 415)
        check(client.post("/products", headers={**ADMIN, "Content-Type": "application/json"}, content=b"{"), 400)
        check(client.post("/products", headers=ADMIN, json={"name": "x"}), 422)
        check(client.post("/products", headers=ADMIN, json={"name": "Standard Widget A"}), 409)
        check(client.get("/products/999999", headers=ADMIN), 404)

        product = check(client.post("/products", headers=ADMIN, json={"name": "HTTP Test Product A"}), 201)
        product_id = product["id"]
        assert check(client.get(f"/products/{product_id}", headers=ADMIN), 200)["name"] == "HTTP Test Product A"
        assert check(client.patch(f"/products/{product_id}", headers=ADMIN, json={"active": True}), 200)["id"] == product_id
        assert any(item["id"] == product_id for item in check(client.get("/products?name=HTTP%20Test%20Product%20A", headers=ADMIN), 200)["items"])

        stock = check(
            client.post("/stock-levels", headers=ADMIN, json={"product_id": product_id, "location": "TEST-A-01", "quantity": 0}),
            201,
        )
        stock_id = stock["id"]
        assert check(client.get("/stock-levels", headers=USER), 200)["items"]
        assert check(client.get(f"/stock-levels/{stock_id}", headers=ADMIN), 200)["quantity"] == 0
        check(client.patch(f"/stock-levels/{stock_id}", headers=ADMIN, json={"location": "TEST-A-02"}), 200)
        check(client.post(f"/stock-levels/{stock_id}/reconcile", headers=ADMIN, json={"actual_quantity": 4}), 200)
        availability = check(client.get(f"/stock/availability?product_id={product_id}&location=TEST-A-02", headers=USER), 200)
        assert availability["available_quantity"] == 4
        check(client.delete(f"/stock-levels/{stock_id}", headers=ADMIN), 409)
        check(client.post(f"/stock-levels/{stock_id}/reconcile", headers=ADMIN, json={"actual_quantity": 0}), 200)
        check(client.delete(f"/stock-levels/{stock_id}", headers=ADMIN), 204)

        movement = check(
            client.post(
                "/stock-movements",
                headers=ADMIN,
                json={"product_id": product_id, "location": "TEST-A-03", "movement_type": "INTAKE", "quantity": 5},
            ),
            201,
        )
        movement_id = movement["id"]
        assert check(client.get("/stock-movements", headers=USER), 200)["items"]
        check(client.patch(f"/stock-movements/{movement_id}", headers=ADMIN, json={"reference": "HTTP persistence check"}), 200)
        assert check(client.get(f"/stock-movements/{movement_id}", headers=ADMIN), 200)["reference"] == "HTTP persistence check"
        assert any(item["id"] == movement_id for item in check(client.get(f"/products/{product_id}/stock-ledger", headers=USER), 200)["items"])
        reversal = check(client.post(f"/stock-movements/{movement_id}/reverse", headers=ADMIN), 200)
        check(client.delete(f"/stock-movements/{reversal['id']}", headers=ADMIN), 204)
        check(client.delete(f"/stock-movements/{movement_id}", headers=ADMIN), 409)
        check(client.delete(f"/products/{product_id}", headers=ADMIN), 204)
