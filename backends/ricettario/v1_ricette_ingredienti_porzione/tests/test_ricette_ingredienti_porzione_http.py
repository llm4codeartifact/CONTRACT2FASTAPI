import os

import httpx


API_BASE_URL = os.environ["API_BASE_URL"].rstrip("/")
USER1 = {"Authorization": "Bearer user1-token-secret-12345"}
USER2 = {"Authorization": "Bearer user2-token-secret-12345"}
ADMIN = {"Authorization": "Bearer admin-token-secret-12345"}


def request(method, path, **kwargs):
    return httpx.request(method, f"{API_BASE_URL}{path}", timeout=10, **kwargs)


def test_recipe_and_component_http_contract():
    assert request("GET", "/health").status_code == 200
    assert request("POST", "/health").status_code == 405
    schema = request("GET", "/openapi.json").json()
    assert {"200", "503"}.issubset(schema["paths"]["/health"]["get"]["responses"])
    assert request("GET", "/recipes").status_code == 401
    assert request("POST", "/ingredients", headers=USER1, json={"name": "Forbidden", "unit": "G"}).status_code == 403
    assert request("GET", "/recipes/1", headers=USER2).status_code == 403

    invalid = request("POST", "/recipes", headers=USER1, json={"title": "x", "instructions": "too short"})
    assert invalid.status_code == 422
    malformed = request("POST", "/recipes", headers={**USER1, "content-type": "application/json"}, content=b"{")
    assert malformed.status_code == 400
    unsupported = request("POST", "/recipes", headers=USER1, content=b"{}")
    assert unsupported.status_code == 415

    calculation = request("GET", "/recipes/1/servings?target_servings=8", headers=USER1)
    assert calculation.status_code == 200
    assert calculation.json()["scaling_factor"] == 2
    assert calculation.json()["components"][0]["scaled_quantity"] == 400

    scaled = request("POST", "/recipes/1/scale", headers=USER1, json={"target_servings": 8})
    assert scaled.status_code == 200
    assert scaled.json()["servings"] == 8
    persisted = request("GET", "/recipes/1", headers=USER1)
    assert persisted.status_code == 200
    assert persisted.json()["servings"] == 8
    assert persisted.json()["components"][0]["quantity"] == 400

    conflict = request("DELETE", "/ingredients/1", headers=ADMIN)
    assert conflict.status_code == 409
