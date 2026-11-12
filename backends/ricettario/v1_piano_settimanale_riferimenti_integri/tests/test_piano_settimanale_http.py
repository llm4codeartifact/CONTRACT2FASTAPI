import os

import httpx


API_BASE_URL = os.environ["API_BASE_URL"].rstrip("/")
USER1 = {"Authorization": "Bearer user1-token-secret-12345"}
USER2 = {"Authorization": "Bearer user2-token-secret-12345"}


def request(method, path, **kwargs):
    return httpx.request(method, f"{API_BASE_URL}{path}", timeout=10, **kwargs)


def test_weekly_plan_http_contract():
    assert request("GET", "/health").status_code == 200
    assert request("POST", "/health").status_code == 405
    schema = request("GET", "/openapi.json").json()
    assert {"200", "503"}.issubset(schema["paths"]["/health"]["get"]["responses"])
    assert request("GET", "/meal-plans").status_code == 401
    assert request("PATCH", "/recipes/1", headers=USER1, json={"title": "Not allowed"}).status_code == 403
    assert request("GET", "/meal-plans/1", headers=USER2).status_code == 403

    invalid = request("POST", "/meal-plans", headers=USER1, json={"week_start_date": "2026-03-03"})
    assert invalid.status_code == 422
    malformed = request("POST", "/meal-plans", headers={**USER1, "content-type": "application/json"}, content=b"{")
    assert malformed.status_code == 400
    unsupported = request("POST", "/meal-plans", headers=USER1, content=b"{}")
    assert unsupported.status_code == 415

    created = request("POST", "/meal-plans", headers=USER1, json={"week_start_date": "2026-03-02", "note": "HTTP plan"})
    assert created.status_code == 201
    plan_id = created.json()["id"]
    assert created.headers["location"] == f"/meal-plans/{plan_id}"

    entry_payload = {"plan_id": plan_id, "recipe_id": 1, "day": "TUESDAY", "meal": "DINNER", "servings": 1}
    entry = request("POST", "/meal-entries", headers=USER1, json=entry_payload)
    assert entry.status_code == 201
    assert request("POST", "/meal-entries", headers=USER1, json=entry_payload).status_code == 409

    patched = request("PATCH", f"/meal-plans/{plan_id}", headers=USER1, json={"note": "Persisted HTTP plan"})
    assert patched.status_code == 200
    persisted = request("GET", f"/meal-plans/{plan_id}", headers=USER1)
    assert persisted.status_code == 200
    assert persisted.json()["note"] == "Persisted HTTP plan"
    shopping = request("GET", f"/meal-plans/{plan_id}/shopping-list", headers=USER1)
    assert shopping.status_code == 200
    assert shopping.json()["items"] == [{"name": "Blueberries", "total_quantity": 50, "unit": "G"}, {"name": "Rolled Oats", "total_quantity": 80, "unit": "G"}]
