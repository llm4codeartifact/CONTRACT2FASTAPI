"""HTTP-only fixtures for the fitness goals API."""
import os

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def assert_json(response, status):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return response.json()


def assert_page(body):
    assert {"items", "limit", "next_after_id"}.issubset(body)
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=API_BASE_URL, timeout=20.0, follow_redirects=False) as value:
        yield value


@pytest.fixture(scope="session")
def user_headers():
    return bearer(os.getenv("API_USER_TOKEN", "user1-token-secret-12345"))


@pytest.fixture(scope="session")
def other_user_headers():
    return bearer(os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345"))


@pytest.fixture(scope="session")
def admin_headers():
    return bearer(os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345"))
