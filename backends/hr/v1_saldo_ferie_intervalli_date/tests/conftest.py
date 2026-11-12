"""HTTP-only fixtures for the leave-balance API."""
import os

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
SECOND_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_json(response: httpx.Response, status_code: int) -> dict:
    assert response.status_code == status_code, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return response.json()


def assert_status(response: httpx.Response, status_code: int) -> None:
    assert response.status_code == status_code, response.text


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
