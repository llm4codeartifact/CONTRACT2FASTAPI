"""HTTP-only fixtures for the temporal-conflict API."""
import os

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
SECOND_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")
VEHICLE_ID = os.getenv("FLEET_TEST_VEHICLE_ID", "1")


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
def vehicle_id():
    if not VEHICLE_ID or not VEHICLE_ID.isdecimal() or int(VEHICLE_ID) <= 0:
        pytest.fail("FLEET_TEST_VEHICLE_ID must declare a positive existing active vehicle id")
    return int(VEHICLE_ID)


@pytest.fixture(scope="session")
def driver(client, admin_headers):
    code = "E2E-TRIP-REQ-015"
    payload = {"employee_code": code, "full_name": "Black Box Trip Driver", "active": True}
    created = client.post("/drivers", headers=admin_headers, json=payload)
    if created.status_code == 201:
        location = created.headers.get("Location")
        assert location and location.endswith("/drivers/" + str(created.json()["id"]))
        return created.json()
    assert created.status_code == 409
    listed = client.get("/drivers", headers=admin_headers, params={"employee_code_prefix": code, "limit": 100})
    assert listed.status_code == 200
    matches = [item for item in listed.json()["items"] if item["employee_code"] == code]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture
def planned_trip(client, user_headers, driver, vehicle_id):
    payload = {
        "driver_id": driver["id"], "vehicle_id": vehicle_id,
        "purpose": "REQ-015 deterministic trip",
        "planned_start": "2031-01-10T10:00:00Z", "planned_end": "2031-01-10T11:00:00Z",
    }
    response = client.post("/trips", headers=user_headers, json=payload)
    assert response.status_code == 201
    assert response.headers.get("Location", "").endswith("/trips/" + str(response.json()["id"]))
    trip = response.json()
    yield trip

    current = client.get(f"/trips/{trip['id']}", headers=user_headers)
    if current.status_code == 200 and current.json()["status"] in {"PLANNED", "IN_PROGRESS"}:
        cleanup = client.post(
            f"/trips/{trip['id']}/cancel",
            headers=user_headers,
            json={
                "cancelled_at": trip["planned_start"],
                "cancellation_reason": "fixture cleanup",
            },
        )
        assert cleanup.status_code == 200
