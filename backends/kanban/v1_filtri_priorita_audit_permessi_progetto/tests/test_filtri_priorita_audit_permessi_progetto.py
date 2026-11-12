from .conftest import assert_json, audit_events, create_project


def test_health_405_and_every_private_route_requires_authentication(client, user_headers):
    before = assert_json(client.get("/tickets", headers=user_headers, params={"limit": 100}), 200)
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert wrong_method.status_code == 405 and "GET" in wrong_method.headers["allow"]
    private_requests = [
        ("GET", "/projects"), ("POST", "/projects"), ("GET", "/projects/1"),
        ("PATCH", "/projects/1"), ("DELETE", "/projects/1"),
        ("GET", "/projects/1/memberships"), ("POST", "/projects/1/memberships"),
        ("PATCH", "/projects/1/memberships/1"), ("DELETE", "/projects/1/memberships/1"),
        ("GET", "/tickets"), ("POST", "/tickets"), ("GET", "/tickets/1"),
        ("PATCH", "/tickets/1"), ("GET", "/audit-events"),
    ]
    for method, path in private_requests:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (method, path, response.text)
    assert assert_json(client.get("/tickets", headers=user_headers, params={"limit": 100}), 200) == before


def test_manager_membership_permissions_ticket_lifecycle_and_audit_postconditions(
    client, user_headers, other_user_headers
):
    created_project = create_project(client, user_headers, "CTST001")
    project_id = created_project["id"]
    memberships = assert_json(
        client.get(f"/projects/{project_id}/memberships", headers=user_headers), 200
    )["items"]
    creator_membership = memberships[0]
    assert creator_membership["account_id"] == 1 and creator_membership["role"] == "MANAGER"
    viewer = assert_json(
        client.post(
            f"/projects/{project_id}/memberships", headers=user_headers,
            json={"account_id": 2, "role": "VIEWER"},
        ),
        201,
    )
    assert_json(client.get(f"/projects/{project_id}", headers=other_user_headers), 200)
    assert client.patch(f"/projects/{project_id}", headers=other_user_headers, json={"name": "Denied"}).status_code == 403
    assert client.post(
        "/tickets", headers=other_user_headers,
        json={"project_id": project_id, "title": "Viewer denied", "priority": "P1"},
    ).status_code == 403
    contributor = assert_json(
        client.patch(
            f"/projects/{project_id}/memberships/{viewer['id']}",
            headers=user_headers, json={"role": "CONTRIBUTOR"},
        ),
        200,
    )
    created_ticket = assert_json(
        client.post(
            "/tickets", headers=other_user_headers,
            json={
                "project_id": project_id,
                "title": "Contributor ticket",
                "description": "searchable audit text",
                "priority": "P1",
                "assignee_membership_id": contributor["id"],
            },
        ),
        201,
    )
    assert created_ticket["creator_account_id"] == 2 and created_ticket["assignee_membership_id"] == contributor["id"]
    patched = assert_json(
        client.patch(f"/tickets/{created_ticket['id']}", headers=other_user_headers, json={"priority": "P0"}), 200
    )
    assert patched["priority"] == "P0"
    events = audit_events(client, user_headers, project_id=project_id, ticket_id=created_ticket["id"])
    assert [event["event_type"] for event in events] == ["TICKET_CREATED", "TICKET_UPDATED"]
    assert events[-1]["changed_fields"] == ["priority"]
    assert client.delete(
        f"/projects/{project_id}/memberships/{contributor['id']}", headers=user_headers
    ).status_code == 204
    assert client.get(f"/tickets/{created_ticket['id']}", headers=other_user_headers).status_code == 403
    assert assert_json(client.get(f"/tickets/{created_ticket['id']}", headers=user_headers), 200) == patched


def test_filters_priority_cursor_admin_scope_and_audit_permissions(
    client, user_headers, other_user_headers, admin_headers
):
    priority_page = assert_json(
        client.get(
            "/tickets", headers=admin_headers,
            params={"project_id": 1, "priority_sort": "true", "limit": 2},
        ),
        200,
    )
    assert [row["priority"] for row in priority_page["items"]] == ["P0", "P1"]
    assert priority_page["next_after_priority"] == "P1" and priority_page["next_after_id"] is not None
    following = assert_json(
        client.get(
            "/tickets", headers=admin_headers,
            params={
                "priority_sort": "true",
                "project_id": 1,
                "after_priority": priority_page["next_after_priority"],
                "after_id": priority_page["next_after_id"],
                "limit": 100,
            },
        ),
        200,
    )
    assert [row["priority"] for row in following["items"]][:3] == ["P2", "P3", "P4"]
    searched = assert_json(client.get("/tickets", headers=admin_headers, params={"q": "performance"}), 200)
    assert [row["id"] for row in searched["items"]] == [2]
    created_by_user_two = assert_json(
        client.get("/tickets", headers=admin_headers, params={"creator_account_id": 2}), 200
    )
    assert all(row["creator_account_id"] == 2 for row in created_by_user_two["items"])
    assert client.get("/tickets", headers=other_user_headers, params={"creator_account_id": 2}).status_code == 403
    assert client.get("/tickets", headers=admin_headers, params={"priority_sort": "true", "after_id": 1}).status_code == 422
    assert client.get("/tickets", headers=admin_headers, params={"q": " "}).status_code == 422
    assert client.get("/audit-events", headers=other_user_headers).status_code == 400
    assert client.get("/audit-events", headers=other_user_headers, params={"project_id": 1}).status_code == 403
    seed_events = audit_events(client, user_headers, project_id=1)
    assert seed_events and all(event["project_id"] == 1 for event in seed_events)


def test_conflicts_validation_and_rejected_writes_preserve_permissions_and_data(client, user_headers):
    membership_before = assert_json(client.get("/projects/1/memberships", headers=user_headers), 200)
    assert client.patch("/projects/1/memberships/1", headers=user_headers, json={"role": "CONTRIBUTOR"}).status_code == 409
    assert client.delete("/projects/1/memberships/1", headers=user_headers).status_code == 409
    assert assert_json(client.get("/projects/1/memberships", headers=user_headers), 200) == membership_before
    tickets_before = assert_json(client.get("/tickets", headers=user_headers, params={"project_id": 1, "limit": 100}), 200)
    assert client.post(
        "/tickets", headers=user_headers,
        json={"project_id": 1, "title": "Viewer assignee", "priority": "P1", "assignee_membership_id": 3},
    ).status_code == 409
    assert assert_json(client.get("/tickets", headers=user_headers, params={"project_id": 1, "limit": 100}), 200) == tickets_before
    assert client.delete("/projects/1", headers=user_headers).status_code == 409
    projects_before = assert_json(client.get("/projects", headers=user_headers, params={"limit": 100}), 200)
    assert client.post("/projects", headers=user_headers, json={"key": "ALPHA", "name": "Duplicate"}).status_code == 409
    assert assert_json(client.get("/projects", headers=user_headers, params={"limit": 100}), 200) == projects_before
    assert client.post(
        "/tickets", headers=user_headers, json={"project_id": 1, "title": "Bad priority", "priority": "P9"}
    ).status_code == 422
    assert client.post("/projects", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post(
        "/projects", headers={**user_headers, "Content-Type": "application/json"}, content=b'{"key":'
    ).status_code == 400


def test_project_update_and_empty_project_deletion_append_audit_events(client, user_headers, admin_headers):
    disposable = create_project(client, user_headers, "CTST002")
    project_id = disposable["id"]
    updated = assert_json(
        client.patch(
            f"/projects/{project_id}", headers=user_headers,
            json={"name": "Updated disposable", "description": "changed"},
        ),
        200,
    )
    assert updated["name"] == "Updated disposable" and updated["description"] == "changed"
    assert client.delete(f"/projects/{project_id}", headers=user_headers).status_code == 204
    assert client.get(f"/projects/{project_id}", headers=user_headers).status_code == 404
    events = audit_events(client, admin_headers, project_id=project_id)
    assert [event["event_type"] for event in events] == [
        "PROJECT_CREATED", "PROJECT_UPDATED", "PROJECT_DELETED"
    ]
