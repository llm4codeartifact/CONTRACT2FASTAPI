import pytest

from .conftest import (
    USER_ONE_ID,
    USER_TWO_ID,
    assert_actuator,
    assert_command,
    assert_error,
    assert_json,
    assert_location,
)


def _matching_actuators(client, headers, serial: str) -> list[dict]:
    page = assert_json(
        client.get("/actuators", headers=headers, params={"serial_prefix": serial, "limit": 100}), 200
    )
    assert set(page) == {"items", "next_after_id"}
    assert [item["id"] for item in page["items"]] == sorted(item["id"] for item in page["items"])
    for item in page["items"]:
        assert_actuator(item)
    return [item for item in page["items"] if item["serial"] == serial]


def test_REQ_2_001_013_actuator_creation_validation_bounds_and_duplicate_are_atomic(
    client, admin_headers, actuator_factory
):
    minimum = actuator_factory(serial="EDGE-1", name="N", actuator_type="SWITCH", initial_value="OFF")
    maximum = actuator_factory(serial="S" * 32, name="N" * 80, actuator_type="DIMMER", initial_value="100")
    assert minimum["current_value"] == "OFF"
    assert maximum["current_value"] == "100"

    invalid_serial = "BAD"
    before_invalid = _matching_actuators(client, admin_headers, invalid_serial)
    invalid = client.post(
        "/actuators",
        headers=admin_headers,
        json={
            "owner_id": USER_ONE_ID,
            "serial": invalid_serial,
            "name": "Invalid serial",
            "type": "SWITCH",
            "initial_value": "OFF",
        },
    )
    assert_error(invalid, 422)
    assert _matching_actuators(client, admin_headers, invalid_serial) == before_invalid

    before_duplicate = assert_json(
        client.get(f"/actuators/{minimum['id']}", headers=admin_headers), 200
    )
    duplicate = client.post(
        "/actuators",
        headers=admin_headers,
        json={
            "owner_id": USER_ONE_ID,
            "serial": minimum["serial"],
            "name": "Other name must not be stored",
            "type": "SWITCH",
            "initial_value": "ON",
        },
    )
    assert_error(duplicate, 409)
    assert assert_json(client.get(f"/actuators/{minimum['id']}", headers=admin_headers), 200) == before_duplicate
    assert _matching_actuators(client, admin_headers, minimum["serial"]) == [before_duplicate]


def test_REQ_2_012_014_actuator_reads_filters_pagination_and_owner_isolation_are_read_only(
    client, admin_headers, user_headers, other_user_headers, actuator_factory
):
    first = actuator_factory(actuator_type="SWITCH", initial_value="OFF")
    second = actuator_factory(actuator_type="SWITCH", initial_value="ON")
    foreign = actuator_factory(owner_id=USER_TWO_ID, actuator_type="DIMMER", initial_value="0")

    own = assert_json(client.get(f"/actuators/{first['id']}", headers=user_headers), 200)
    assert own == first
    before = assert_json(client.get(f"/actuators/{first['id']}", headers=admin_headers), 200)

    forbidden = client.get(f"/actuators/{first['id']}", headers=other_user_headers)
    assert_error(forbidden, 403)
    assert assert_json(client.get(f"/actuators/{first['id']}", headers=admin_headers), 200) == before
    assert_error(client.get("/actuators/0", headers=admin_headers), 404)

    prefix = first["serial"][:-3]
    page_one = assert_json(
        client.get(
            "/actuators",
            headers=user_headers,
            params={"type": "SWITCH", "serial_prefix": prefix, "owner_id": USER_ONE_ID, "limit": 1},
        ),
        200,
    )
    assert len(page_one["items"]) == 1
    assert page_one["items"][0]["id"] in {first["id"], second["id"]}
    assert page_one["next_after_id"] == page_one["items"][0]["id"]
    page_two = assert_json(
        client.get(
            "/actuators",
            headers=user_headers,
            params={"type": "SWITCH", "serial_prefix": prefix, "owner_id": USER_ONE_ID,
                    "after_id": page_one["next_after_id"], "limit": 100},
        ),
        200,
    )
    ids = [item["id"] for item in page_one["items"] + page_two["items"]]
    assert ids == sorted(ids)
    assert first["id"] in ids and second["id"] in ids
    assert len(ids) == len(set(ids))
    assert page_two["next_after_id"] is None

    assert_error(client.get("/actuators", headers=user_headers, params={"owner_id": USER_TWO_ID}), 403)
    assert_error(client.get("/actuators", headers=admin_headers, params={"owner_id": 0}), 404)
    assert_error(client.get("/actuators", headers=admin_headers, params={"limit": 0}), 422)
    assert_error(client.get("/actuators", headers=admin_headers, params={"after_id": 0}), 422)
    assert assert_json(client.get(f"/actuators/{first['id']}", headers=admin_headers), 200) == before
    assert assert_json(client.get(f"/actuators/{foreign['id']}", headers=admin_headers), 200) == foreign


def test_REQ_2_015_patch_is_idempotent_preserves_command_history_and_rejects_non_admins(
    client, admin_headers, user_headers, actuator_factory, command_list
):
    actuator = actuator_factory(actuator_type="SWITCH", initial_value="OFF")
    command = assert_json(
        client.post(f"/actuators/{actuator['id']}/commands", headers=user_headers, json={"value": "ON"}), 201
    )
    assert_command(command)
    commands_before = command_list(client, admin_headers, actuator["id"])["items"]

    denied = client.patch(
        f"/actuators/{actuator['id']}", headers=user_headers, json={"name": "Forbidden mutation"}
    )
    assert_error(denied, 403)
    unchanged = assert_json(client.get(f"/actuators/{actuator['id']}", headers=admin_headers), 200)
    assert unchanged["name"] == actuator["name"] and unchanged["current_value"] == "ON"
    assert command_list(client, admin_headers, actuator["id"])["items"] == commands_before

    patch = {"name": "Patched actuator"}
    first = assert_json(
        client.patch(f"/actuators/{actuator['id']}", headers=admin_headers, json=patch), 200
    )
    assert first["name"] == patch["name"] and first["current_value"] == "ON"
    second = assert_json(
        client.patch(f"/actuators/{actuator['id']}", headers=admin_headers, json=patch), 200
    )
    assert second == first
    assert command_list(client, admin_headers, actuator["id"])["items"] == commands_before

    invalid = client.patch(f"/actuators/{actuator['id']}", headers=admin_headers, json={})
    assert_error(invalid, 422)
    assert assert_json(client.get(f"/actuators/{actuator['id']}", headers=admin_headers), 200) == first
    assert command_list(client, admin_headers, actuator["id"])["items"] == commands_before


def test_REQ_2_016_delete_unreferenced_actuator_and_conflicts_preserve_relations(
    client, admin_headers, user_headers, actuator_factory, rule_factory, command_list
):
    disposable = actuator_factory()
    deleted = client.delete(f"/actuators/{disposable['id']}", headers=admin_headers)
    assert deleted.status_code == 204 and deleted.content == b""
    assert_error(client.get(f"/actuators/{disposable['id']}", headers=admin_headers), 404)
    assert_error(client.delete(f"/actuators/{disposable['id']}", headers=admin_headers), 404)

    action_target = actuator_factory()
    rule = rule_factory(action_target)
    action_target_before = assert_json(client.get(f"/actuators/{action_target['id']}", headers=admin_headers), 200)
    rule_before = assert_json(client.get(f"/automation-rules/{rule['id']}", headers=admin_headers), 200)
    action_conflict = client.delete(f"/actuators/{action_target['id']}", headers=admin_headers)
    assert_error(action_conflict, 409)
    assert assert_json(client.get(f"/actuators/{action_target['id']}", headers=admin_headers), 200) == action_target_before
    assert assert_json(client.get(f"/automation-rules/{rule['id']}", headers=admin_headers), 200) == rule_before

    command_target = actuator_factory()
    command = assert_json(
        client.post(f"/actuators/{command_target['id']}/commands", headers=user_headers, json={"value": "ON"}), 201
    )
    assert_command(command)
    commands_before = command_list(client, admin_headers, command_target["id"])["items"]
    command_conflict = client.delete(f"/actuators/{command_target['id']}", headers=admin_headers)
    assert_error(command_conflict, 409)
    assert command_list(client, admin_headers, command_target["id"])["items"] == commands_before
    assert assert_json(client.get(f"/actuators/{command_target['id']}", headers=admin_headers), 200)["current_value"] == "ON"


@pytest.mark.parametrize(
    ("actuator_type", "initial_value", "valid_values", "invalid_value"),
    [
        ("SWITCH", "OFF", ("ON", "OFF"), "1"),
        ("DIMMER", "0", ("0", "100"), "101"),
        ("THERMOSTAT", "5.0", ("5.0", "35.0"), "35.1"),
    ],
)
def test_REQ_2_002_008_017_manual_commands_validate_type_boundaries_and_are_atomic(
    client, admin_headers, user_headers, actuator_factory, command_list,
    actuator_type, initial_value, valid_values, invalid_value,
):
    actuator = actuator_factory(actuator_type=actuator_type, initial_value=initial_value)
    for value in valid_values:
        response = client.post(f"/actuators/{actuator['id']}/commands", headers=user_headers, json={"value": value})
        command = assert_json(response, 201)
        assert response.headers.get("location"), "A created command must expose Location"
        assert_command(command)
        assert command["actuator_id"] == actuator["id"]
        assert command["origin"] == "MANUAL"
        assert command["execution_id"] is None
        assert command["command_value"] == value and command["status"] == "APPLIED"
        assert assert_json(client.get(f"/actuators/{actuator['id']}", headers=admin_headers), 200)["current_value"] == value

    before_actuator = assert_json(client.get(f"/actuators/{actuator['id']}", headers=admin_headers), 200)
    before_commands = command_list(client, admin_headers, actuator["id"])["items"]
    invalid = client.post(
        f"/actuators/{actuator['id']}/commands", headers=user_headers, json={"value": invalid_value}
    )
    assert_error(invalid, 422)
    assert assert_json(client.get(f"/actuators/{actuator['id']}", headers=admin_headers), 200) == before_actuator
    assert command_list(client, admin_headers, actuator["id"])["items"] == before_commands


def test_REQ_2_018_command_history_filters_pagination_and_owner_checks_are_read_only(
    client, admin_headers, user_headers, other_user_headers, actuator_factory, command_list
):
    actuator = actuator_factory()
    for value in ("ON", "OFF"):
        assert_json(client.post(f"/actuators/{actuator['id']}/commands", headers=user_headers, json={"value": value}), 201)
    before = command_list(client, admin_headers, actuator["id"])["items"]

    page = command_list(client, user_headers, actuator["id"], origin="MANUAL", limit=1)
    assert len(page["items"]) == 1 and page["items"][0]["origin"] == "MANUAL"
    assert page["next_after_id"] == page["items"][0]["id"]
    following = command_list(client, user_headers, actuator["id"], origin="MANUAL", after_id=page["next_after_id"], limit=100)
    assert [item["id"] for item in page["items"] + following["items"]] == [item["id"] for item in before]
    assert following["next_after_id"] is None

    assert_error(client.get(f"/actuators/{actuator['id']}/commands", headers=other_user_headers), 403)
    assert_error(client.post(f"/actuators/{actuator['id']}/commands", headers=other_user_headers, json={"value": "ON"}), 403)
    assert_error(client.get(f"/actuators/{actuator['id']}/commands", headers=admin_headers, params={"origin": "OTHER"}), 422)
    assert_error(client.get(f"/actuators/{actuator['id']}/commands", headers=admin_headers, params={"after_id": 0}), 422)
    assert_error(client.get("/actuators/0/commands", headers=admin_headers), 404)
    assert command_list(client, admin_headers, actuator["id"])["items"] == before


def test_REQ_2_payload_media_malformed_and_over_10_mib_are_rejected_without_actuator_write(
    client, admin_headers
):
    serial = "BODYLIMIT"
    before = _matching_actuators(client, admin_headers, serial)

    unsupported = client.post("/actuators", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}")
    assert_error(unsupported, 415)
    malformed = client.post(
        "/actuators", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"owner_id": 1'
    )
    assert_error(malformed, 400)
    oversized = b'{"owner_id":1,"serial":"BODYLIMIT' + b"A" * (10 * 1024 * 1024) + b'"}'
    too_large = client.post(
        "/actuators", headers={**admin_headers, "Content-Type": "application/json"}, content=oversized
    )
    assert_error(too_large, 413)
    assert _matching_actuators(client, admin_headers, serial) == before
