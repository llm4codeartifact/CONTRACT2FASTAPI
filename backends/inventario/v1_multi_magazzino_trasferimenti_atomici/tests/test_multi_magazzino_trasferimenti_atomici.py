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
        assert check(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
        check(client.post("/health"), 405)


def test_authentication_and_read_authorization():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        check(client.get("/warehouses"), 401)
        assert check(client.get("/warehouses", headers=USER), 200)["items"]
        check(client.post("/warehouses", headers=USER, json={"code": "USER-WH", "name": "User Warehouse"}), 403)


def test_transfer_lifecycle_validation_conflicts_and_persistence():
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        check(client.post("/warehouses", headers={**ADMIN, "Content-Type": "text/plain"}, content=b"{}"), 415)
        check(client.post("/warehouses", headers={**ADMIN, "Content-Type": "application/json"}, content=b"{"), 400)
        check(client.post("/warehouses", headers=ADMIN, json={"code": "X", "name": "Invalid"}), 422)
        check(client.post("/warehouses", headers=ADMIN, json={"code": "WH-MILANO", "name": "Duplicate"}), 409)
        check(client.get("/warehouses/999999", headers=ADMIN), 404)

        warehouse = check(client.post("/warehouses", headers=ADMIN, json={"code": "HTTP-B-WH", "name": "HTTP Test Warehouse"}), 201)
        warehouse_id = warehouse["id"]
        assert check(client.get(f"/warehouses/{warehouse_id}", headers=ADMIN), 200)["code"] == "HTTP-B-WH"
        check(client.patch(f"/warehouses/{warehouse_id}", headers=ADMIN, json={"name": "HTTP Test Warehouse Updated"}), 200)

        product = check(client.post("/products", headers=ADMIN, json={"sku": "HTTP-B-PROD", "name": "HTTP Test Product"}), 201)
        product_id = product["id"]
        assert check(client.get("/products", headers=USER), 200)["items"]
        assert check(client.get(f"/products/{product_id}", headers=ADMIN), 200)["sku"] == "HTTP-B-PROD"
        check(client.patch(f"/products/{product_id}", headers=ADMIN, json={"name": "HTTP Test Product Updated"}), 200)

        check(
            client.post("/transfers", headers=ADMIN, json={"source_warehouse_id": 1, "dest_warehouse_id": 1, "product_id": 1, "quantity": 1}),
            422,
        )
        transfer = check(
            client.post("/transfers", headers=ADMIN, json={"source_warehouse_id": 1, "dest_warehouse_id": 2, "product_id": 1, "quantity": 12}),
            201,
        )
        transfer_id = transfer["id"]
        assert check(client.get("/transfers", headers=USER), 200)["items"]
        assert check(client.get(f"/transfers/{transfer_id}", headers=ADMIN), 200)["status"] == "DRAFT"
        check(client.patch(f"/transfers/{transfer_id}", headers=ADMIN, json={"quantity": 12}), 200)
        assert any(item["id"] == transfer_id for item in check(client.get("/warehouses/1/transfer-conflicts", headers=USER), 200)["items"])
        assert check(client.post(f"/transfers/{transfer_id}/dispatch", headers=ADMIN), 200)["status"] == "DISPATCHED"
        assert check(client.post(f"/transfers/{transfer_id}/receive", headers=ADMIN), 200)["status"] == "RECEIVED"
        assert check(client.post(f"/transfers/{transfer_id}/receive", headers=ADMIN), 200)["status"] == "RECEIVED"

        cancellable = check(
            client.post("/transfers", headers=ADMIN, json={"source_warehouse_id": 1, "dest_warehouse_id": 2, "product_id": 1, "quantity": 1}),
            201,
        )
        assert check(client.post(f"/transfers/{cancellable['id']}/cancel", headers=ADMIN), 200)["status"] == "CANCELLED"

        deletable = check(
            client.post("/transfers", headers=ADMIN, json={"source_warehouse_id": 1, "dest_warehouse_id": 2, "product_id": 1, "quantity": 1}),
            201,
        )
        check(client.delete(f"/transfers/{deletable['id']}", headers=ADMIN), 204)
        check(client.get(f"/transfers/{deletable['id']}", headers=ADMIN), 404)

        insufficient = check(
            client.post("/transfers", headers=ADMIN, json={"source_warehouse_id": 1, "dest_warehouse_id": 2, "product_id": 1, "quantity": 100000}),
            201,
        )
        check(client.post(f"/transfers/{insufficient['id']}/dispatch", headers=ADMIN), 409)
        check(client.delete(f"/transfers/{insufficient['id']}", headers=ADMIN), 204)
        check(client.delete(f"/products/{product_id}", headers=ADMIN), 204)
        check(client.delete(f"/warehouses/{warehouse_id}", headers=ADMIN), 204)
