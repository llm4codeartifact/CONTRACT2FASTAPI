import os

import httpx


API_BASE_URL = os.environ["API_BASE_URL"].rstrip("/")
USER1 = {"Authorization": "Bearer user1-token-secret-12345"}
USER2 = {"Authorization": "Bearer user2-token-secret-12345"}
ADMIN = {"Authorization": "Bearer admin-token-secret-12345"}


def request(method, path, **kwargs):
    return httpx.request(method, f"{API_BASE_URL}{path}", timeout=10, **kwargs)


def test_nutrition_tags_and_ownership_http_contract():
    assert request("GET", "/health").status_code == 200
    assert request("POST", "/health").status_code == 405
    schema = request("GET", "/openapi.json").json()
    assert {"200", "503"}.issubset(schema["paths"]["/health"]["get"]["responses"])
    assert set(schema["paths"]["/recipes/search"]) == {"get"}
    assert request("GET", "/ingredients").status_code == 401
    assert request("POST", "/tags", headers=USER1, json={"name": "Forbidden", "dietary_type": "VEGAN"}).status_code == 403
    assert request("PATCH", "/recipes/1", headers=USER2, json={"title": "Other user"}).status_code == 403

    invalid = request("POST", "/recipes", headers=USER1, json={"title": "x", "instructions": "too short"})
    assert invalid.status_code == 422
    malformed = request("POST", "/recipes", headers={**USER1, "content-type": "application/json"}, content=b"{")
    assert malformed.status_code == 400
    unsupported = request("POST", "/recipes", headers=USER1, content=b"{}")
    assert unsupported.status_code == 415

    ingredient = request(
        "POST",
        "/ingredients",
        headers=ADMIN,
        json={"name": "HTTP Test Lentils", "calories_kcal": 100, "protein_g": 10, "carbs_g": 20, "fat_g": 1},
    )
    assert ingredient.status_code == 201
    ingredient_id = ingredient.json()["id"]
    recipe = request(
        "POST",
        "/recipes",
        headers=USER1,
        json={
            "title": "HTTP Lentil Bowl",
            "instructions": "Cook the lentils thoroughly and serve the bowl warm.",
            "servings": 2,
            "components": [{"ingredient_id": ingredient_id, "quantity_grams": 200}],
        },
    )
    assert recipe.status_code == 201
    recipe_id = recipe.json()["id"]

    nutrition = request("GET", f"/recipes/{recipe_id}/nutrition", headers=USER1)
    assert nutrition.status_code == 200
    assert nutrition.json()["total_calories_kcal"] == 200
    assert nutrition.json()["per_serving_protein_g"] == 10
    assert request("POST", f"/recipes/{recipe_id}/tags", headers=USER1, json={"tag_id": 3}).status_code == 201
    assert request("POST", f"/recipes/{recipe_id}/tags", headers=USER1, json={"tag_id": 3}).status_code == 409

    patched = request("PATCH", f"/recipes/{recipe_id}", headers=USER1, json={"title": "Persisted Lentil Bowl"})
    assert patched.status_code == 200
    persisted = request("GET", f"/recipes/{recipe_id}", headers=USER1)
    assert persisted.status_code == 200
    assert persisted.json()["title"] == "Persisted Lentil Bowl"
    search = request("GET", "/recipes/search?q=Persisted%20Lentil", headers=USER1)
    assert search.status_code == 200
    assert [item["id"] for item in search.json()["items"]] == [recipe_id]
    assert request("GET", "/recipes/search?tag_ids=0", headers=USER1).status_code == 422
    assert request("POST", "/recipes/search", headers=USER1, json={}).status_code == 405
