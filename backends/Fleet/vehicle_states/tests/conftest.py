"""Deterministic HTTP-only fixtures for the vehicle-state backend."""

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
    with httpx.Client(base_url=API_BASE_URL, timeout=30.0, follow_redirects=False) as value:
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


def create_vehicle(client, admin_headers, license_plate: str) -> dict:
    response = client.post(
        "/vehicles",
        headers=admin_headers,
        json={
            "license_plate": license_plate,
            "manufacturer": "Test Manufacturer",
            "model": "Test Model",
            "year": 2030,
            "active": True,
        },
    )
    assert response.status_code == 201
    assert response.headers["Location"].endswith("/vehicles/" + str(response.json()["id"]))
    return response.json()


def create_scheduled_maintenance(client, user_headers, vehicle_id: int, title: str) -> dict:
    response = client.post(
        "/maintenances",
        headers=user_headers,
        json={
            "vehicle_id": vehicle_id,
            "type": "INSPECTION",
            "title": title,
            "description": "Deterministic black-box test maintenance",
            "scheduled_start": "2035-06-10T10:00:00Z",
            "scheduled_end": "2035-06-10T11:00:00Z",
        },
    )
    assert response.status_code == 201
    assert response.headers["Location"].endswith("/maintenances/" + str(response.json()["id"]))
    return response.json()


@pytest.fixture
def scheduled_maintenance(client, admin_headers, user_headers):
    vehicle = create_vehicle(client, admin_headers, "TS-201")
    return create_scheduled_maintenance(client, user_headers, vehicle["id"], "Scheduled maintenance test")


@pytest.fixture
def lifecycle_maintenance(client, admin_headers, user_headers, request):
    license_plate = "TS-203" if "026_cancel" in request.node.name else "TS-202"
    vehicle = create_vehicle(client, admin_headers, license_plate)
    maintenance = create_scheduled_maintenance(client, user_headers, vehicle["id"], "Lifecycle maintenance test")
    transition = client.post(
        f"/vehicles/{vehicle['id']}/state-transitions",
        headers=admin_headers,
        json={
            "target_state": "MAINTENANCE",
            "effective_at": "2035-06-10T10:00:00Z",
            "reason": "Maintenance test setup",
        },
    )
    assert transition.status_code == 201
    return vehicle, maintenance
