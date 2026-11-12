import pytest

from .conftest import assert_error, assert_json


def test_REQ_2_011_health_is_public_read_only_and_advertises_get(client, admin_headers):
    """Health must not mutate any publicly observable domain collection."""
    before = {
        "actuators": assert_json(client.get("/actuators", headers=admin_headers, params={"limit": 100}), 200),
        "rules": assert_json(client.get("/automation-rules", headers=admin_headers, params={"limit": 100}), 200),
        "executions": assert_json(client.get("/automation-executions", headers=admin_headers, params={"limit": 100}), 200),
    }

    health = assert_json(client.get("/health"), 200)
    assert set(health) == {"status", "database"}
    assert health["status"] == "ok"
    schema = assert_json(client.get("/openapi.json"), 200)
    assert set(schema["paths"]["/health"]["get"]["responses"]) == {"200", "503"}
    assert isinstance(health["database"], str) and health["database"]

    after = {
        "actuators": assert_json(client.get("/actuators", headers=admin_headers, params={"limit": 100}), 200),
        "rules": assert_json(client.get("/automation-rules", headers=admin_headers, params={"limit": 100}), 200),
        "executions": assert_json(client.get("/automation-executions", headers=admin_headers, params={"limit": 100}), 200),
    }
    assert after == before

    method_not_allowed = client.post("/health")
    assert_error(method_not_allowed, 405)
    assert "GET" in method_not_allowed.headers["allow"].upper()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/actuators"),
        ("POST", "/actuators"),
        ("GET", "/actuators/0"),
        ("PATCH", "/actuators/0"),
        ("DELETE", "/actuators/0"),
        ("GET", "/actuators/0/commands"),
        ("POST", "/actuators/0/commands"),
        ("GET", "/automation-rules"),
        ("POST", "/automation-rules"),
        ("GET", "/automation-rules/0"),
        ("PUT", "/automation-rules/0"),
        ("DELETE", "/automation-rules/0"),
        ("POST", "/automation-rules/0/enable"),
        ("POST", "/automation-rules/0/disable"),
        ("POST", "/automation-events"),
        ("GET", "/automation-executions"),
    ],
)
def test_REQ_2_010_every_private_operation_denies_missing_bearer(client, method, path):
    response = client.request(method, path, json={})
    assert_error(response, 401)
