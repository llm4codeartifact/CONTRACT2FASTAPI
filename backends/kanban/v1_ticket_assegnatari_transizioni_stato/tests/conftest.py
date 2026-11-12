"""HTTP-only fixtures for the tickets, assignees, and transitions API."""
import os

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
OTHER_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def assert_json(response, status):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return response.json()


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=20.0, follow_redirects=False) as value:
        yield value


@pytest.fixture(scope="session")
def user_headers():
    return bearer(USER_TOKEN)


@pytest.fixture(scope="session")
def other_user_headers():
    return bearer(OTHER_USER_TOKEN)


@pytest.fixture(scope="session")
def admin_headers():
    return bearer(ADMIN_TOKEN)


def project(client, headers, key, name="Test Project"):
    response = client.post("/projects", headers=headers, json={"key": key, "name": name})
    body = assert_json(response, 201)
    assert response.headers["location"].endswith(f"/projects/{body['id']}")
    assert body["key"] == key and body["creator_account_id"] == 1
    return body


def ticket(client, headers, project_id, title="Valid ticket", points=3):
    response = client.post(
        "/tickets",
        headers=headers,
        json={"project_id": project_id, "title": title, "estimated_points": points},
    )
    body = assert_json(response, 201)
    assert response.headers["location"].endswith(f"/tickets/{body['id']}")
    assert body["project_id"] == project_id and body["status"] == "TODO"
    return body
