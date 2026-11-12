from .conftest import (
    USER_ONE_ID,
    USER_TWO_ID,
    assert_command,
    assert_error,
    assert_execution,
    assert_json,
    assert_location,
    assert_rule,
    assert_utc_second,
)


def _rules(client, headers, **params) -> dict:
    body = assert_json(client.get("/automation-rules", headers=headers, params=params), 200)
    assert set(body) == {"items", "next_after_id"}
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    for item in body["items"]:
        assert_rule(item)
    return body


def _executions(client, headers, **params) -> dict:
    body = assert_json(client.get("/automation-executions", headers=headers, params=params), 200)
    assert set(body) == {"items", "next_after_id"}
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    for item in body["items"]:
        assert_execution(item)
    return body


def _event_payload(event_id: str, *, source_kind: str, value: float, occurred_at: str, owner_id=None) -> dict:
    payload = {
        "client_event_id": event_id,
        "source_kind": source_kind,
        "value": value,
        "occurred_at": occurred_at,
    }
    if owner_id is not None:
        payload["owner_id"] = owner_id
    return payload


def _assert_event(body: dict, payload: dict, owner_id: int) -> None:
    assert set(body) == {"id", "owner_id", "client_event_id", "source_kind", "value", "occurred_at"}
    assert isinstance(body["id"], int) and body["id"] > 0
    assert body["owner_id"] == owner_id
    assert body["client_event_id"] == payload["client_event_id"]
    assert body["source_kind"] == payload["source_kind"]
    assert body["value"] == payload["value"]
    assert_utc_second(body["occurred_at"])
    assert body["occurred_at"].replace("+00:00", "Z") == payload["occurred_at"]


def test_REQ_2_003_004_020_rule_creation_validates_bounds_actions_owners_and_duplicate_names(
    client, admin_headers, user_headers, actuator_factory, rule_factory
):
    minimum_target = actuator_factory()
    minimum = rule_factory(
        minimum_target,
        name="MIN",
        source_kind="MOTION",
        comparator="EQ",
        threshold=-50,
        cooldown_seconds=0,
    )
    assert minimum["enabled"] is True

    targets = [actuator_factory() for _ in range(5)]
    maximum_actions = [
        {"order": index, "actuator_id": target["id"], "command_value": "ON"}
        for index, target in enumerate(targets, start=1)
    ]
    maximum = rule_factory(
        targets[0],
        actions=maximum_actions,
        source_kind="TEMPERATURE",
        comparator="GTE",
        threshold=5000,
        cooldown_seconds=86400,
    )
    assert [action["order"] for action in maximum["actions"]] == [1, 2, 3, 4, 5]
    assert [action["actuator_id"] for action in maximum["actions"]] == [target["id"] for target in targets]

    rules_before = _rules(client, user_headers, owner_id=USER_ONE_ID, limit=100)
    short_name = client.post(
        "/automation-rules",
        headers=user_headers,
        json={
            "name": "NO", "source_kind": "TEMPERATURE", "comparator": "GT", "threshold": 1,
            "cooldown_seconds": 0,
            "actions": [{"order": 1, "actuator_id": minimum_target["id"], "command_value": "ON"}],
        },
    )
    assert_error(short_name, 422)
    assert _rules(client, user_headers, owner_id=USER_ONE_ID, limit=100) == rules_before

    duplicate_action = client.post(
        "/automation-rules",
        headers=user_headers,
        json={
            "name": "Duplicate action", "source_kind": "TEMPERATURE", "comparator": "GT", "threshold": 1,
            "cooldown_seconds": 0,
            "actions": [
                {"order": 1, "actuator_id": minimum_target["id"], "command_value": "ON"},
                {"order": 2, "actuator_id": minimum_target["id"], "command_value": "OFF"},
            ],
        },
    )
    assert_error(duplicate_action, 422)
    assert _rules(client, user_headers, owner_id=USER_ONE_ID, limit=100) == rules_before

    foreign_target = actuator_factory(owner_id=USER_TWO_ID)
    foreign_before = assert_json(client.get(f"/actuators/{foreign_target['id']}", headers=admin_headers), 200)
    foreign_action = client.post(
        "/automation-rules",
        headers=user_headers,
        json={
            "name": "Foreign target", "source_kind": "TEMPERATURE", "comparator": "GT", "threshold": 1,
            "cooldown_seconds": 0,
            "actions": [{"order": 1, "actuator_id": foreign_target["id"], "command_value": "ON"}],
        },
    )
    assert_error(foreign_action, 422)
    assert assert_json(client.get(f"/actuators/{foreign_target['id']}", headers=admin_headers), 200) == foreign_before
    assert _rules(client, user_headers, owner_id=USER_ONE_ID, limit=100) == rules_before

    minimum_before = assert_json(client.get(f"/automation-rules/{minimum['id']}", headers=user_headers), 200)
    duplicate_name = client.post(
        "/automation-rules",
        headers=user_headers,
        json={
            "name": minimum["name"], "source_kind": "TEMPERATURE", "comparator": "GT", "threshold": 1,
            "cooldown_seconds": 0,
            "actions": [{"order": 1, "actuator_id": minimum_target["id"], "command_value": "ON"}],
        },
    )
    assert_error(duplicate_name, 409)
    assert assert_json(client.get(f"/automation-rules/{minimum['id']}", headers=user_headers), 200) == minimum_before


def test_REQ_2_019_021_rule_reads_filters_pagination_and_owner_isolation_are_read_only(
    client, admin_headers, user_headers, other_user_headers, actuator_factory, rule_factory
):
    target = actuator_factory()
    own = rule_factory(target, source_kind="HUMIDITY", comparator="LTE", threshold=75)
    foreign_target = actuator_factory(owner_id=USER_TWO_ID)
    foreign = rule_factory(foreign_target, owner_id=USER_TWO_ID, as_admin=True, source_kind="HUMIDITY", comparator="LTE", threshold=75)

    assert assert_json(client.get(f"/automation-rules/{own['id']}", headers=user_headers), 200) == own
    own_before = assert_json(client.get(f"/automation-rules/{own['id']}", headers=admin_headers), 200)
    assert_error(client.get(f"/automation-rules/{own['id']}", headers=other_user_headers), 403)
    assert_error(client.get(f"/automation-rules/{foreign['id']}", headers=user_headers), 403)
    assert_error(client.get("/automation-rules/0", headers=admin_headers), 404)
    assert assert_json(client.get(f"/automation-rules/{own['id']}", headers=admin_headers), 200) == own_before

    page_one = _rules(client, user_headers, enabled=True, source_kind="HUMIDITY", owner_id=USER_ONE_ID, limit=1)
    assert len(page_one["items"]) == 1
    page_two = _rules(
        client, user_headers, enabled=True, source_kind="HUMIDITY", owner_id=USER_ONE_ID,
        after_id=page_one["next_after_id"], limit=100,
    ) if page_one["next_after_id"] else {"items": [], "next_after_id": None}
    ids = [item["id"] for item in page_one["items"] + page_two["items"]]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    assert own["id"] in ids
    assert page_two["next_after_id"] is None
    assert_error(client.get("/automation-rules", headers=user_headers, params={"owner_id": USER_TWO_ID}), 403)
    assert_error(client.get("/automation-rules", headers=admin_headers, params={"owner_id": 0}), 404)
    assert_error(client.get("/automation-rules", headers=admin_headers, params={"enabled": "not-a-bool"}), 422)
    assert_error(client.get("/automation-rules", headers=admin_headers, params={"after_id": 0}), 422)
    assert assert_json(client.get(f"/automation-rules/{own['id']}", headers=admin_headers), 200) == own_before


def test_REQ_2_022_rule_replace_is_complete_idempotent_and_rolls_back_invalid_or_conflicting_writes(
    client, admin_headers, user_headers, actuator_factory, rule_factory
):
    first_target = actuator_factory()
    second_target = actuator_factory()
    first = rule_factory(first_target)
    other = rule_factory(second_target)
    other_before = assert_json(client.get(f"/automation-rules/{other['id']}", headers=admin_headers), 200)
    replacement = {
        "name": "Replacement rule",
        "source_kind": "HUMIDITY",
        "comparator": "LTE",
        "threshold": 40,
        "cooldown_seconds": 60,
        "enabled": False,
        "actions": [{"order": 1, "actuator_id": second_target["id"], "command_value": "ON"}],
    }
    replaced = assert_json(
        client.put(f"/automation-rules/{first['id']}", headers=user_headers, json=replacement), 200
    )
    assert_rule(replaced)
    assert replaced["name"] == replacement["name"]
    assert replaced["enabled"] is False
    assert replaced["actions"][0]["actuator_id"] == second_target["id"]
    persisted = assert_json(client.get(f"/automation-rules/{first['id']}", headers=admin_headers), 200)
    assert persisted == replaced

    repeated = assert_json(
        client.put(f"/automation-rules/{first['id']}", headers=user_headers, json=replacement), 200
    )
    assert repeated == persisted
    assert assert_json(client.get(f"/automation-rules/{first['id']}", headers=admin_headers), 200) == persisted

    invalid = client.put(f"/automation-rules/{first['id']}", headers=user_headers, json={})
    assert_error(invalid, 422)
    assert assert_json(client.get(f"/automation-rules/{first['id']}", headers=admin_headers), 200) == persisted

    collision = dict(replacement, name=other["name"])
    duplicate = client.put(f"/automation-rules/{first['id']}", headers=user_headers, json=collision)
    assert_error(duplicate, 409)
    assert assert_json(client.get(f"/automation-rules/{first['id']}", headers=admin_headers), 200) == persisted
    assert assert_json(client.get(f"/automation-rules/{other['id']}", headers=admin_headers), 200) == other_before


def test_REQ_2_023_024_025_rule_enable_disable_and_delete_contracts(client, admin_headers, user_headers, actuator_factory, rule_factory):
    toggled = rule_factory(actuator_factory())
    original_actions = toggled["actions"]
    disabled = assert_json(client.post(f"/automation-rules/{toggled['id']}/disable", headers=user_headers), 200)
    assert_rule(disabled)
    assert disabled["enabled"] is False and disabled["actions"] == original_actions
    assert_json(client.post(f"/automation-rules/{toggled['id']}/disable", headers=user_headers), 200)
    assert assert_json(client.get(f"/automation-rules/{toggled['id']}", headers=admin_headers), 200) == disabled
    enabled = assert_json(client.post(f"/automation-rules/{toggled['id']}/enable", headers=user_headers), 200)
    assert enabled["enabled"] is True and enabled["actions"] == original_actions
    assert_json(client.post(f"/automation-rules/{toggled['id']}/enable", headers=user_headers), 200)
    assert assert_json(client.get(f"/automation-rules/{toggled['id']}", headers=admin_headers), 200) == enabled

    unexecuted = rule_factory(actuator_factory())
    deleted = client.delete(f"/automation-rules/{unexecuted['id']}", headers=user_headers)
    assert deleted.status_code == 204 and deleted.content == b""
    assert_error(client.get(f"/automation-rules/{unexecuted['id']}", headers=admin_headers), 404)
    assert_error(client.delete(f"/automation-rules/{unexecuted['id']}", headers=user_headers), 404)
    assert_error(client.post("/automation-rules/0/enable", headers=admin_headers), 404)
    assert_error(client.post("/automation-rules/0/disable", headers=admin_headers), 404)


def test_REQ_2_005_026_event_bounds_idempotency_conflict_and_atomic_effects(
    client, admin_headers, user_headers, actuator_factory, rule_factory, command_list, tagged_values
):
    lower_payload = _event_payload(
        tagged_values["event_id"](1), source_kind="MOTION", value=-50, occurred_at="2035-01-01T00:00:00Z"
    )
    lower_response = client.post("/automation-events", headers=user_headers, json=lower_payload)
    lower = assert_json(lower_response, 201)
    assert lower_response.headers.get("location")
    _assert_event(lower, lower_payload, USER_ONE_ID)
    upper_payload = _event_payload(
        tagged_values["event_id"](2), source_kind="MOTION", value=5000, occurred_at="2035-01-01T00:01:00Z"
    )
    _assert_event(assert_json(client.post("/automation-events", headers=user_headers, json=upper_payload), 201), upper_payload, USER_ONE_ID)

    rejected_key = tagged_values["event_id"](3)
    invalid_payload = _event_payload(rejected_key, source_kind="MOTION", value=5000.1, occurred_at="2035-01-01T00:02:00Z")
    invalid = client.post("/automation-events", headers=user_headers, json=invalid_payload)
    assert_error(invalid, 422)
    accepted_after_error = _event_payload(rejected_key, source_kind="MOTION", value=5000, occurred_at="2035-01-01T00:02:00Z")
    assert_json(client.post("/automation-events", headers=user_headers, json=accepted_after_error), 201)

    target = actuator_factory()
    rule = rule_factory(target, threshold=20)
    payload = _event_payload(
        tagged_values["event_id"](4), source_kind="TEMPERATURE", value=21, occurred_at="2035-02-01T00:00:00Z"
    )
    created_response = client.post("/automation-events", headers=user_headers, json=payload)
    created = assert_json(created_response, 201)
    assert created_response.headers.get("location")
    _assert_event(created, payload, USER_ONE_ID)
    executions_before = _executions(client, admin_headers, rule_id=rule["id"], client_event_id=payload["client_event_id"])["items"]
    assert len(executions_before) == 1 and executions_before[0]["outcome"] == "APPLIED"
    commands_before = command_list(client, admin_headers, target["id"])["items"]
    assert any(
        command["origin"] == "RULE" and command["execution_id"] == executions_before[0]["id"]
        and command["command_value"] == "ON" for command in commands_before
    )
    actuator_before = assert_json(client.get(f"/actuators/{target['id']}", headers=admin_headers), 200)
    assert actuator_before["current_value"] == "ON"

    repeated = assert_json(client.post("/automation-events", headers=user_headers, json=payload), 200)
    assert repeated == created
    assert _executions(client, admin_headers, rule_id=rule["id"], client_event_id=payload["client_event_id"])["items"] == executions_before
    assert command_list(client, admin_headers, target["id"])["items"] == commands_before
    assert assert_json(client.get(f"/actuators/{target['id']}", headers=admin_headers), 200) == actuator_before

    conflict_payload = dict(payload, value=22)
    conflict = client.post("/automation-events", headers=user_headers, json=conflict_payload)
    assert_error(conflict, 409)
    assert _executions(client, admin_headers, rule_id=rule["id"], client_event_id=payload["client_event_id"])["items"] == executions_before
    assert command_list(client, admin_headers, target["id"])["items"] == commands_before
    assert assert_json(client.get(f"/actuators/{target['id']}", headers=admin_headers), 200) == actuator_before


def test_REQ_2_006_007_ordered_execution_cooldown_disabled_rules_and_executed_delete_conflict(
    client, admin_headers, user_headers, actuator_factory, rule_factory, command_list, tagged_values
):
    first_target, second_target = actuator_factory(), actuator_factory()
    first_rule = rule_factory(first_target, source_kind="MOTION", comparator="EQ", threshold=1234)
    second_rule = rule_factory(second_target, source_kind="MOTION", comparator="EQ", threshold=1234)
    ordered_payload = _event_payload(
        tagged_values["event_id"](1), source_kind="MOTION", value=1234, occurred_at="2036-01-01T00:00:00Z"
    )
    assert_json(client.post("/automation-events", headers=user_headers, json=ordered_payload), 201)
    ordered = _executions(client, admin_headers, client_event_id=ordered_payload["client_event_id"])["items"]
    assert [item["rule_id"] for item in ordered] == sorted([first_rule["id"], second_rule["id"]])
    assert all(item["outcome"] == "APPLIED" for item in ordered)
    for target, rule in ((first_target, first_rule), (second_target, second_rule)):
        execution = next(item for item in ordered if item["rule_id"] == rule["id"])
        assert assert_json(client.get(f"/actuators/{target['id']}", headers=admin_headers), 200)["current_value"] == "ON"
        assert any(command["execution_id"] == execution["id"] for command in command_list(client, admin_headers, target["id"])["items"])

    cooldown_target = actuator_factory()
    cooldown_rule = rule_factory(cooldown_target, source_kind="HUMIDITY", threshold=1, cooldown_seconds=60)
    first_payload = _event_payload(
        tagged_values["event_id"](2), source_kind="HUMIDITY", value=2, occurred_at="2036-02-01T00:00:00Z"
    )
    assert_json(client.post("/automation-events", headers=user_headers, json=first_payload), 201)
    commands_before = command_list(client, admin_headers, cooldown_target["id"])["items"]
    value_before = assert_json(client.get(f"/actuators/{cooldown_target['id']}", headers=admin_headers), 200)
    second_payload = _event_payload(
        tagged_values["event_id"](3), source_kind="HUMIDITY", value=2, occurred_at="2036-02-01T00:00:30Z"
    )
    assert_json(client.post("/automation-events", headers=user_headers, json=second_payload), 201)
    suppressed = _executions(client, admin_headers, rule_id=cooldown_rule["id"], client_event_id=second_payload["client_event_id"])["items"]
    assert len(suppressed) == 1 and suppressed[0]["outcome"] == "SUPPRESSED"
    assert command_list(client, admin_headers, cooldown_target["id"])["items"] == commands_before
    assert assert_json(client.get(f"/actuators/{cooldown_target['id']}", headers=admin_headers), 200) == value_before

    executed_before_delete = assert_json(client.get(f"/automation-rules/{first_rule['id']}", headers=admin_headers), 200)
    delete_executed = client.delete(f"/automation-rules/{first_rule['id']}", headers=user_headers)
    assert_error(delete_executed, 409)
    assert assert_json(client.get(f"/automation-rules/{first_rule['id']}", headers=admin_headers), 200) == executed_before_delete

    disabled_target = actuator_factory()
    disabled_rule = rule_factory(disabled_target, source_kind="MOTION", comparator="EQ", threshold=4999)
    assert_json(client.post(f"/automation-rules/{disabled_rule['id']}/disable", headers=user_headers), 200)
    disabled_before = assert_json(client.get(f"/actuators/{disabled_target['id']}", headers=admin_headers), 200)
    disabled_payload = _event_payload(
        tagged_values["event_id"](4), source_kind="MOTION", value=4999, occurred_at="2036-03-01T00:00:00Z"
    )
    assert_json(client.post("/automation-events", headers=user_headers, json=disabled_payload), 201)
    assert _executions(client, admin_headers, rule_id=disabled_rule["id"], client_event_id=disabled_payload["client_event_id"])["items"] == []
    assert assert_json(client.get(f"/actuators/{disabled_target['id']}", headers=admin_headers), 200) == disabled_before


def test_REQ_2_027_execution_filters_pagination_validation_and_visibility_are_read_only(
    client, admin_headers, user_headers, other_user_headers, actuator_factory, rule_factory, command_list, tagged_values
):
    target = actuator_factory()
    rule = rule_factory(target, source_kind="TEMPERATURE", threshold=10)
    first = _event_payload(tagged_values["event_id"](1), source_kind="TEMPERATURE", value=11, occurred_at="2037-01-01T00:00:00Z")
    second = _event_payload(tagged_values["event_id"](2), source_kind="TEMPERATURE", value=12, occurred_at="2037-01-01T00:01:00Z")
    assert_json(client.post("/automation-events", headers=user_headers, json=first), 201)
    assert_json(client.post("/automation-events", headers=user_headers, json=second), 201)
    all_for_rule = _executions(client, user_headers, rule_id=rule["id"], outcome="APPLIED", limit=100)
    assert len(all_for_rule["items"]) == 2
    page = _executions(client, user_headers, rule_id=rule["id"], outcome="APPLIED", limit=1)
    assert len(page["items"]) == 1 and page["next_after_id"] == page["items"][0]["id"]
    following = _executions(
        client, user_headers, rule_id=rule["id"], outcome="APPLIED", after_id=page["next_after_id"], limit=100
    )
    assert [item["id"] for item in page["items"] + following["items"]] == [item["id"] for item in all_for_rule["items"]]
    assert following["next_after_id"] is None
    assert len(_executions(client, user_headers, client_event_id=first["client_event_id"])["items"]) >= 1

    executions_before = _executions(client, admin_headers, rule_id=rule["id"], limit=100)["items"]
    commands_before = command_list(client, admin_headers, target["id"])["items"]
    target_before = assert_json(client.get(f"/actuators/{target['id']}", headers=admin_headers), 200)
    assert_error(client.get("/automation-executions", headers=other_user_headers, params={"rule_id": rule["id"]}), 403)
    assert_error(client.get("/automation-executions", headers=admin_headers, params={"rule_id": 0}), 404)
    assert_error(client.get("/automation-executions", headers=admin_headers, params={"outcome": "OTHER"}), 422)
    assert_error(client.get("/automation-executions", headers=admin_headers, params={"after_id": 0}), 422)
    assert _executions(client, admin_headers, rule_id=rule["id"], limit=100)["items"] == executions_before
    assert command_list(client, admin_headers, target["id"])["items"] == commands_before
    assert assert_json(client.get(f"/actuators/{target['id']}", headers=admin_headers), 200) == target_before
