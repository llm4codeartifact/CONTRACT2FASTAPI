import pytest

from .conftest import (
    USER_ONE_ID, USER_TWO_ID, alerts, assert_json, assert_policy, assert_reading,
    assert_sensor, assert_status, readings,
)


def _sensors(client, headers, **params):
    body = assert_json(client.get("/sensors", headers=headers, params=params), 200)
    assert {"items", "next_after_id"}.issubset(body)
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    for item in body["items"]: assert_sensor(item)
    return body


def _post_reading(client, headers, sensor, value, captured_at):
    response = client.post(f"/sensors/{sensor['id']}/readings", headers=headers, json={"value": value, "captured_at": captured_at})
    body = assert_json(response, 201)
    assert_reading(body); assert response.headers.get("location")
    assert body["sensor_id"] == sensor["id"] and body["value"] == value
    return body


def test_REQ_2_011_health_405_and_private_routes_require_authentication(client, admin_headers):
    before = {"sensors": _sensors(client, admin_headers, limit=100), "alerts": alerts(client, admin_headers, limit=100)}
    health = assert_json(client.get("/health"), 200)
    assert set(health) == {"status", "database"} and health["status"] == "ok"
    schema = assert_json(client.get("/openapi.json"), 200)
    assert set(schema["paths"]["/health"]["get"]["responses"]) == {"200", "503"}
    assert {"sensors": _sensors(client, admin_headers, limit=100), "alerts": alerts(client, admin_headers, limit=100)} == before
    wrong_method = client.post("/health")
    assert_status(wrong_method, 405); assert "GET" in wrong_method.headers["allow"].upper()
    private = [
        ("GET", "/sensors"), ("POST", "/sensors"), ("GET", "/sensors/0"), ("PATCH", "/sensors/0"), ("DELETE", "/sensors/0"),
        ("GET", "/sensors/0/thresholds"), ("POST", "/sensors/0/thresholds"), ("PATCH", "/sensors/0/thresholds"), ("DELETE", "/sensors/0/thresholds"),
        ("POST", "/sensors/0/readings"), ("GET", "/sensors/0/readings"), ("GET", "/readings/0"), ("GET", "/sensors/0/latest"), ("GET", "/sensors/0/range"), ("GET", "/alerts"),
    ]
    for method, path in private: assert_status(client.request(method, path, json={}), 401)


@pytest.mark.parametrize(
    ("sensor_type", "minimum", "maximum", "invalid"),
    [
        ("TEMPERATURE", -50.0, 150.0, 150.1),
        ("HUMIDITY", 0.0, 100.0, 100.1),
        ("CO2", 0, 5000, 5001),
        ("ILLUMINANCE", 0, 200000, 200001),
    ],
)
def test_REQ_2_001_002_003_sensor_and_reading_physical_boundaries_are_atomic(
    client, admin_headers, user_headers, sensor_factory, sensor_type, minimum, maximum, invalid
):
    sensor = sensor_factory(sensor_type=sensor_type)
    low = _post_reading(client, user_headers, sensor, minimum, "2038-01-01T00:00:00Z")
    high = _post_reading(client, user_headers, sensor, maximum, "2038-01-01T00:01:00Z")
    assert [low["captured_at"], high["captured_at"]] == ["2038-01-01T00:00:00Z", "2038-01-01T00:01:00Z"]
    before = readings(client, admin_headers, sensor["id"], limit=100)["items"]
    invalid_response = client.post(f"/sensors/{sensor['id']}/readings", headers=user_headers, json={"value": invalid, "captured_at": "2038-01-01T00:02:00Z"})
    assert_status(invalid_response, 422)
    assert readings(client, admin_headers, sensor["id"], limit=100)["items"] == before


def test_REQ_2_012_013_014_015_016_sensor_crud_authorization_and_body_errors(
    client, admin_headers, user_headers, other_user_headers, sensor_factory
):
    sensor = sensor_factory(serial="EDGE-1", name="N")
    maximum = sensor_factory(serial="S" * 32, name="N" * 80, sensor_type="CO2")
    assert_sensor(maximum)
    assert_status(client.post("/sensors", headers=user_headers, json={"owner_id": USER_ONE_ID, "serial": "USER-1", "name": "Denied", "type": "TEMPERATURE"}), 403)
    assert_status(client.post("/sensors", headers=admin_headers, json={"owner_id": USER_ONE_ID, "serial": "bad", "name": "Invalid", "type": "TEMPERATURE"}), 422)
    before_duplicate = assert_json(client.get(f"/sensors/{sensor['id']}", headers=admin_headers), 200)
    assert_status(client.post("/sensors", headers=admin_headers, json={"owner_id": USER_ONE_ID, "serial": sensor["serial"], "name": "Duplicate", "type": "TEMPERATURE"}), 409)
    assert assert_json(client.get(f"/sensors/{sensor['id']}", headers=admin_headers), 200) == before_duplicate

    assert assert_json(client.get(f"/sensors/{sensor['id']}", headers=user_headers), 200) == sensor
    assert_status(client.get(f"/sensors/{sensor['id']}", headers=other_user_headers), 403)
    assert_status(client.get("/sensors/0", headers=admin_headers), 404)
    page = _sensors(client, user_headers, type="TEMPERATURE", serial_prefix=sensor["serial"][:-2], owner_id=USER_ONE_ID, limit=1)
    assert page["items"] and page["next_after_id"] is None
    assert_status(client.get("/sensors", headers=user_headers, params={"owner_id": USER_TWO_ID}), 403)
    assert_status(client.get("/sensors", headers=admin_headers, params={"owner_id": 0}), 404)
    assert_status(client.get("/sensors", headers=admin_headers, params={"limit": 0}), 422)
    assert_status(client.get("/sensors", headers=admin_headers, params={"after_id": 0}), 422)

    patch = {"name": "Patched sensor"}
    changed = assert_json(client.patch(f"/sensors/{sensor['id']}", headers=admin_headers, json=patch), 200)
    assert changed["name"] == patch["name"]
    assert assert_json(client.patch(f"/sensors/{sensor['id']}", headers=admin_headers, json=patch), 200) == changed
    assert_status(client.patch(f"/sensors/{sensor['id']}", headers=admin_headers, json={}), 422)
    assert assert_json(client.get(f"/sensors/{sensor['id']}", headers=admin_headers), 200) == changed
    assert_status(client.patch(f"/sensors/{sensor['id']}", headers=user_headers, json={"name": "Forbidden"}), 403)

    disposable = sensor_factory()
    assert client.delete(f"/sensors/{disposable['id']}", headers=admin_headers).status_code == 204
    assert_status(client.delete(f"/sensors/{disposable['id']}", headers=admin_headers), 404)
    assert_status(client.delete(f"/sensors/{sensor['id']}", headers=user_headers), 403)

    assert_status(client.post("/sensors", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}"), 415)
    assert_status(client.post("/sensors", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"owner_id":1'), 400)
    oversized = b'{"serial":"OVERSIZE' + b"A" * (10 * 1024 * 1024) + b'"}'
    assert_status(client.post("/sensors", headers={**admin_headers, "Content-Type": "application/json"}, content=oversized), 413)


def test_REQ_2_005_006_008_009_threshold_reading_alert_lifecycle_and_immutability(
    client, admin_headers, user_headers, other_user_headers, sensor_factory
):
    sensor = sensor_factory()
    no_policy = assert_json(client.get(f"/sensors/{sensor['id']}/thresholds", headers=user_headers), 200)
    assert no_policy is None
    create = client.post(f"/sensors/{sensor['id']}/thresholds", headers=user_headers, json={"lower_bound": 0, "upper_bound": 100})
    policy = assert_json(create, 201); assert_policy(policy); assert create.headers.get("location")
    policy_before = assert_json(client.get(f"/sensors/{sensor['id']}/thresholds", headers=admin_headers), 200)
    assert policy_before == policy
    assert_status(client.post(f"/sensors/{sensor['id']}/thresholds", headers=user_headers, json={"lower_bound": 1}), 409)
    assert_status(client.patch(f"/sensors/{sensor['id']}/thresholds", headers=user_headers, json={"lower_bound": 100}), 422)
    assert assert_json(client.get(f"/sensors/{sensor['id']}/thresholds", headers=admin_headers), 200) == policy_before

    normal = _post_reading(client, user_headers, sensor, 50, "2039-01-01T00:00:00Z")
    high = _post_reading(client, user_headers, sensor, 101, "2039-01-01T00:01:00Z")
    low = _post_reading(client, user_headers, sensor, -1, "2039-01-01T00:02:00Z")
    alert_rows = alerts(client, admin_headers, sensor_id=sensor["id"], limit=100)["items"]
    assert len(alert_rows) == 2
    assert {(row["reading_id"], row["direction"]) for row in alert_rows} == {(high["id"], "ABOVE"), (low["id"], "BELOW")}
    assert all(row["threshold_lower"] == 0 and row["threshold_upper"] == 100 for row in alert_rows)
    assert alerts(client, user_headers, sensor_id=sensor["id"], direction="ABOVE", resolved=False, limit=100)["items"] == [next(row for row in alert_rows if row["direction"] == "ABOVE")]

    patched = assert_json(client.patch(f"/sensors/{sensor['id']}/thresholds", headers=user_headers, json={"upper_bound": 90}), 200)
    assert_policy(patched); assert patched["upper_bound"] == 90
    assert assert_json(client.patch(f"/sensors/{sensor['id']}/thresholds", headers=user_headers, json={"upper_bound": 90}), 200) == patched
    assert alerts(client, admin_headers, sensor_id=sensor["id"], limit=100)["items"] == alert_rows
    assert client.delete(f"/sensors/{sensor['id']}/thresholds", headers=user_headers).status_code == 204
    assert assert_json(client.get(f"/sensors/{sensor['id']}/thresholds", headers=user_headers), 200) is None
    assert alerts(client, admin_headers, sensor_id=sensor["id"], limit=100)["items"] == alert_rows
    assert_status(client.delete(f"/sensors/{sensor['id']}/thresholds", headers=user_headers), 404)
    assert_status(client.delete(f"/sensors/{sensor['id']}", headers=admin_headers), 409)

    readings_before = readings(client, admin_headers, sensor["id"], limit=100)["items"]
    assert_status(client.post(f"/sensors/{sensor['id']}/readings", headers=user_headers, json={"value": 151, "captured_at": "2039-01-01T00:03:00Z"}), 422)
    assert readings(client, admin_headers, sensor["id"], limit=100)["items"] == readings_before
    accepted = _post_reading(client, user_headers, sensor, 60, "2039-01-01T00:03:00Z")
    assert_status(client.post(f"/sensors/{sensor['id']}/readings", headers=user_headers, json={"value": 61, "captured_at": "2039-01-01T00:03:00Z"}), 409)
    assert assert_json(client.get(f"/readings/{accepted['id']}", headers=user_headers), 200) == accepted
    assert_status(client.get(f"/readings/{accepted['id']}", headers=other_user_headers), 403)


def test_REQ_2_007_022_023_024_025_026_range_latest_lists_and_failed_queries_are_read_only(
    client, admin_headers, user_headers, other_user_headers, sensor_factory
):
    sensor = sensor_factory()
    values = [10, 20, 30]
    created = [_post_reading(client, user_headers, sensor, value, f"2040-01-01T00:0{index}:00Z") for index, value in enumerate(values)]
    listing = readings(client, user_headers, sensor["id"], **{"from": "2040-01-01T00:00:00Z", "to": "2040-01-01T00:03:00Z", "limit": 100})
    assert [item["id"] for item in listing["items"]] == [item["id"] for item in created]
    page = readings(client, user_headers, sensor["id"], limit=1)
    following = readings(client, user_headers, sensor["id"], after_id=page["next_after_id"], limit=100)
    assert [item["id"] for item in page["items"] + following["items"]] == [item["id"] for item in created]
    latest = assert_json(client.get(f"/sensors/{sensor['id']}/latest", headers=user_headers), 200)
    assert latest == created[-1]
    range_body = assert_json(client.get(f"/sensors/{sensor['id']}/range", headers=user_headers, params={"from": "2040-01-01T00:00:00Z", "to": "2040-01-01T00:03:00Z"}), 200)
    assert range_body["sensor_id"] == sensor["id"] and range_body["count"] == 3
    assert range_body["min"] == 10 and range_body["max"] == 30 and range_body["average"] == 20.0
    assert [item["id"] for item in range_body["readings"]] == [item["id"] for item in created]

    before = readings(client, admin_headers, sensor["id"], limit=100)["items"]
    assert_status(client.get(f"/sensors/{sensor['id']}/readings", headers=user_headers, params={"from": "2040-01-02T00:00:00Z", "to": "2040-01-01T00:00:00Z"}), 422)
    assert_status(client.get(f"/sensors/{sensor['id']}/range", headers=user_headers, params={"from": "2040-01-01T00:00:00Z", "to": "2040-02-02T00:00:00Z"}), 422)
    assert_status(client.get(f"/sensors/{sensor['id']}/range", headers=other_user_headers, params={"from": "2040-01-01T00:00:00Z", "to": "2040-01-02T00:00:00Z"}), 403)
    assert_status(client.get("/alerts", headers=admin_headers, params={"sensor_id": 0}), 404)
    assert_status(client.get("/alerts", headers=admin_headers, params={"direction": "SIDEWAYS"}), 422)
    assert_status(client.get(f"/sensors/{sensor['id']}/readings", headers=user_headers, params={"after_id": 0}), 422)
    assert_status(client.get("/alerts", headers=admin_headers, params={"after_id": 0}), 422)
    assert readings(client, admin_headers, sensor["id"], limit=100)["items"] == before
