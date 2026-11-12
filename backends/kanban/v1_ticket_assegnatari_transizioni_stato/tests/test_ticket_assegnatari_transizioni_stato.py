from .conftest import assert_json, project, ticket


def test_health_405_and_every_private_route_requires_authentication(client, user_headers):
    before = assert_json(client.get("/projects", headers=user_headers), 200)
    health = assert_json(client.get("/health"), 200)
    assert health == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert wrong_method.status_code == 405 and "GET" in wrong_method.headers["allow"]
    private_requests = [
        ("GET", "/projects"), ("POST", "/projects"), ("GET", "/projects/1"),
        ("PATCH", "/projects/1"), ("DELETE", "/projects/1"),
        ("GET", "/projects/1/memberships"), ("POST", "/projects/1/memberships"),
        ("DELETE", "/projects/1/memberships/1"), ("GET", "/tickets"),
        ("POST", "/tickets"), ("GET", "/tickets/1"), ("PATCH", "/tickets/1"),
        ("POST", "/tickets/1/assignee"), ("POST", "/tickets/1/transitions"),
    ]
    for method, path in private_requests:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (method, path, response.text)
    assert assert_json(client.get("/projects", headers=user_headers), 200) == before


def test_project_membership_and_ticket_writes_persist_with_expected_relations(
    client, user_headers
):
    created_project = project(client, user_headers, "ATST001", "Atomic Test")
    project_id = created_project["id"]
    updated_project = assert_json(
        client.patch(
            f"/projects/{project_id}", headers=user_headers,
            json={"description": "updated project description"},
        ),
        200,
    )
    assert updated_project["description"] == "updated project description"
    memberships = assert_json(
        client.get(f"/projects/{project_id}/memberships", headers=user_headers), 200
    )
    creator_membership = memberships["items"][0]
    assert creator_membership["account_id"] == 1 and creator_membership["active"] is True
    added = assert_json(
        client.post(
            f"/projects/{project_id}/memberships", headers=user_headers, json={"account_id": 2}
        ),
        201,
    )
    assert added["project_id"] == project_id and added["account_id"] == 2
    created_ticket = ticket(client, user_headers, project_id, "Boundary story", 21)
    patched = assert_json(
        client.patch(
            f"/tickets/{created_ticket['id']}",
            headers=user_headers,
            json={"title": "Boundary story updated", "description": "kept"},
        ),
        200,
    )
    assert patched["status"] == "TODO" and patched["assignee_membership_id"] is None
    assigned = assert_json(
        client.post(
            f"/tickets/{created_ticket['id']}/assignee",
            headers=user_headers,
            json={"assignee_membership_id": creator_membership["id"]},
        ),
        200,
    )
    transitioned = assert_json(
        client.post(
            f"/tickets/{created_ticket['id']}/transitions",
            headers=user_headers,
            json={"target_status": "IN_PROGRESS", "transition_note": "started"},
        ),
        200,
    )
    assert transitioned["status"] == "IN_PROGRESS"
    assert assert_json(client.get(f"/tickets/{created_ticket['id']}", headers=user_headers), 200) == transitioned
    page = assert_json(client.get("/tickets", headers=user_headers, params={"project_id": project_id}), 200)
    assert any(row["id"] == created_ticket["id"] for row in page["items"])
    assert [row["id"] for row in page["items"]] == sorted(row["id"] for row in page["items"])
    assert assigned["assignee_membership_id"] == creator_membership["id"]
    assert client.delete(
        f"/projects/{project_id}/memberships/{added['id']}", headers=user_headers
    ).status_code == 204
    inactive_members = assert_json(
        client.get(
            f"/projects/{project_id}/memberships", headers=user_headers,
            params={"active": "false"},
        ),
        200,
    )
    assert [row["id"] for row in inactive_members["items"]] == [added["id"]]


def test_ownership_and_member_assignment_permissions_are_enforced(
    client, user_headers, other_user_headers, admin_headers
):
    assert_json(client.get("/projects/1", headers=other_user_headers), 200)
    assert client.patch("/projects/1", headers=other_user_headers, json={"name": "Denied"}).status_code == 403
    original_ticket = assert_json(client.get("/tickets/1", headers=user_headers), 200)
    assert client.patch("/tickets/1", headers=other_user_headers, json={"title": "Denied"}).status_code == 403
    assert client.post("/tickets/1/assignee", headers=other_user_headers, json={"assignee_membership_id": 2}).status_code == 403
    assert client.post("/tickets/1/transitions", headers=other_user_headers, json={"target_status": "IN_PROGRESS"}).status_code == 403
    assert assert_json(client.get("/tickets/1", headers=user_headers), 200) == original_ticket
    admin_change = assert_json(
        client.patch("/tickets/1", headers=admin_headers, json={"description": "admin change"}), 200
    )
    assert admin_change["description"] == "admin change"


def test_conflicts_validation_and_failed_writes_preserve_existing_state(client, user_headers):
    seed_ticket = assert_json(client.get("/tickets/4", headers=user_headers), 200)
    assert client.post(
        "/tickets/4/transitions", headers=user_headers, json={"target_status": "TODO"}
    ).status_code == 409
    assert assert_json(client.get("/tickets/4", headers=user_headers), 200) == seed_ticket
    assert client.delete("/projects/1/memberships/2", headers=user_headers).status_code == 409
    assert assert_json(client.get("/tickets/2", headers=user_headers), 200)["assignee_membership_id"] == 2
    before_projects = assert_json(client.get("/projects", headers=user_headers, params={"limit": 100}), 200)
    assert client.post("/projects", headers=user_headers, json={"key": "ALPHA", "name": "Duplicate"}).status_code == 409
    assert assert_json(client.get("/projects", headers=user_headers, params={"limit": 100}), 200) == before_projects
    assert client.post(
        "/tickets", headers=user_headers, json={"project_id": 1, "title": "Too many", "estimated_points": 22}
    ).status_code == 422
    assert client.patch("/projects/1", headers=user_headers, json={}).status_code == 400
    assert client.post("/projects", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post(
        "/projects", headers={**user_headers, "Content-Type": "application/json"}, content=b'{"key":'
    ).status_code == 400


def test_delete_is_soft_and_nonempty_project_deletion_conflicts(client, user_headers):
    disposable = project(client, user_headers, "ATST002", "Disposable")
    assert client.delete(f"/projects/{disposable['id']}", headers=user_headers).status_code == 204
    assert client.get(f"/projects/{disposable['id']}", headers=user_headers).status_code == 404
    assert client.delete("/projects/1", headers=user_headers).status_code == 409
