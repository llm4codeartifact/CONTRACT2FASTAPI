"""HTTP-only fixtures for the maintenance-planning API."""
import os

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
SECOND_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=20.0, follow_redirects=False) as value:
        yield value


@pytest.fixture(scope="session")
def admin_headers():
    return bearer(ADMIN_TOKEN)


@pytest.fixture(scope="session")
def user_headers():
    return bearer(USER_TOKEN)


@pytest.fixture(scope="session")
def other_user_headers():
    return bearer(SECOND_USER_TOKEN)


@pytest.fixture(scope="session")
def available_vehicle(client, admin_headers):
    response = client.get("/vehicles", headers=admin_headers, params={"active": "true", "limit": 100})
    assert response.status_code == 200
    candidates = [item for item in response.json()["items"] if item["active"]]
    if not candidates:
        pytest.fail("the API must contain a declared active test vehicle")
    return candidates[0]


@pytest.fixture
def scheduled_maintenance(client, user_headers, available_vehicle):
    payload = {
        "vehicle_id": available_vehicle["id"], "type": "INSPECTION",
        "title": "REQ-014 deterministic inspection",
        "description": "Black-box scheduled maintenance",
        "planned_start": "2032-02-10T10:00:00Z", "planned_end": "2032-02-10T11:00:00Z",
    }
    response = client.post("/maintenances", headers=user_headers, json=payload)
    assert response.status_code == 201
    assert response.headers.get("Location", "").endswith("/maintenances/" + str(response.json()["id"]))
    maintenance = response.json()
    yield maintenance

    current = client.get(f"/maintenances/{maintenance['id']}", headers=user_headers)
    if current.status_code == 200 and current.json()["status"] in {"SCHEDULED", "IN_PROGRESS"}:
        cleanup = client.post(
            f"/maintenances/{maintenance['id']}/cancel",
            headers=user_headers,
            json={
                "cancelled_at": maintenance["planned_start"],
                "cancellation_reason": "fixture cleanup",
            },
        )
        assert cleanup.status_code == 200
