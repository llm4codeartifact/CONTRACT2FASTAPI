"""HTTP-only fixtures for the overbooking reservations API."""
import os
import re

import httpx
import pytest


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8420").rstrip("/")
ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
USER_TOKEN = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
SECOND_USER_TOKEN = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def ok(response, status):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].split(";", 1)[0] == "application/json"
    return response.json()


def error(response, status):
    body = ok(response, status)
    assert set(body) == {"detail", "status_code", "error_type"}
    assert body["status_code"] == status
    return body


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


@pytest.fixture
def table_code(request):
    suffix = re.sub(r"[^A-Z0-9]", "", request.node.name.upper())[:10]
    return f"X{suffix}" or "XTABLE"
