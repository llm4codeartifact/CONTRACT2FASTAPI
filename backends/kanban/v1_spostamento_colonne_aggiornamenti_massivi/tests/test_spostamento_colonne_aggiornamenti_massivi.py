from .conftest import assert_json, columns, create_project, tickets


def test_health_405_and_every_private_route_requires_authentication(client, user_headers):
    before = tickets(client, user_headers, project_id=1)
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert wrong_method.status_code == 405 and "GET" in wrong_method.headers["allow"]
    private_requests = [
        ("GET", "/projects"), ("POST", "/projects"), ("GET", "/projects/1"),
        ("PATCH", "/projects/1"), ("DELETE", "/projects/1"),
        ("GET", "/projects/1/columns"), ("POST", "/projects/1/columns"),
        ("GET", "/columns/1"), ("PATCH", "/columns/1"), ("DELETE", "/columns/1"),
        ("GET", "/tickets"), ("POST", "/tickets"), ("GET", "/tickets/1"),
        ("POST", "/tickets/1/move"), ("POST", "/projects/1/tickets/bulk-move"),
    ]
    for method, path in private_requests:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (method, path, response.text)
    assert tickets(client, user_headers, project_id=1) == before


def test_project_column_crud_and_ownership_are_http_enforced(
    client, user_headers, other_user_headers, admin_headers
):
    owned = create_project(client, user_headers, "BTST001")
    assert_json(client.get(f"/projects/{owned['id']}", headers=user_headers), 200)
    renamed = assert_json(
        client.patch(f"/projects/{owned['id']}", headers=user_headers, json={"name": "Renamed project"}), 200
    )
    assert renamed["name"] == "Renamed project"
    own_page = assert_json(client.get("/projects", headers=user_headers, params={"key_prefix": "BTST"}), 200)
    assert [row["id"] for row in own_page["items"]] == [owned["id"]]
    assert client.get("/projects/1", headers=other_user_headers).status_code == 403
    assert client.post(
        "/projects/1/columns", headers=other_user_headers,
        json={"name": "Denied", "position": 1},
    ).status_code == 403
    before = columns(client, user_headers, 1)
    inserted = assert_json(
        client.post(
            "/projects/1/columns", headers=user_headers,
            json={"name": "Middle Test", "position": 2, "wip_limit": 0},
        ),
        201,
    )
    assert inserted["position"] == 2
    after = columns(client, user_headers, 1)
    assert [row["position"] for row in sorted(after, key=lambda row: row["position"])] == list(range(1, len(after) + 1))
    assert client.post(
        "/projects/1/columns", headers=user_headers,
        json={"name": "Middle Test", "position": 1},
    ).status_code == 409
    assert columns(client, user_headers, 1) == after
    patched = assert_json(
        client.patch(f"/columns/{inserted['id']}", headers=user_headers, json={"name": "Middle Renamed"}), 200
    )
    assert patched["name"] == "Middle Renamed"
    assert_json(client.get(f"/columns/{inserted['id']}", headers=admin_headers), 200)
    empty = assert_json(
        client.post("/projects/1/columns", headers=user_headers, json={"name": "Empty Test", "position": len(after) + 1}), 201
    )
    assert client.delete(f"/columns/{empty['id']}", headers=user_headers).status_code == 204
    assert client.get(f"/columns/{empty['id']}", headers=user_headers).status_code == 404
    assert client.delete("/columns/1", headers=user_headers).status_code == 409
    assert client.delete("/projects/1", headers=user_headers).status_code == 409
    assert len(columns(client, user_headers, 1)) == len(before) + 1
    disposable = create_project(client, user_headers, "BTST002")
    assert client.delete(f"/projects/{disposable['id']}", headers=user_headers).status_code == 204
    assert client.get(f"/projects/{disposable['id']}", headers=user_headers).status_code == 404


def test_ticket_create_move_and_bulk_move_leave_contiguous_persistent_orders(client, user_headers):
    created = assert_json(
        client.post(
            "/tickets", headers=user_headers,
            json={"project_id": 1, "column_id": 2, "title": "Movable ticket", "description": "x"},
        ),
        201,
    )
    assert created["position"] >= 1
    moved = assert_json(
        client.post(
            f"/tickets/{created['id']}/move", headers=user_headers,
            json={"target_column_id": 3, "after_ticket_id": 4},
        ),
        200,
    )
    assert moved["column_id"] == 3 and moved["position"] == 2
    bulk = assert_json(
        client.post(
            "/projects/1/tickets/bulk-move", headers=user_headers,
            json={"ticket_ids": [1, 3], "target_column_id": 3, "placement": "BOTTOM"},
        ),
        200,
    )
    assert [row["id"] for row in bulk] == [1, 3]
    for column_id in (1, 2, 3):
        ordered = sorted(tickets(client, user_headers, column_id=column_id), key=lambda row: row["position"])
        assert [row["position"] for row in ordered] == list(range(1, len(ordered) + 1))
    persisted = assert_json(client.get(f"/tickets/{created['id']}", headers=user_headers), 200)
    assert persisted == moved


def test_invalid_moves_wip_and_contract_failures_rollback_all_ticket_ordering(client, user_headers):
    assert_json(
        client.post(
            "/tickets", headers=user_headers,
            json={"project_id": 1, "column_id": 1, "title": "Rollback ticket"},
        ),
        201,
    )
    before = tickets(client, user_headers, project_id=1)
    assert client.patch("/columns/1", headers=user_headers, json={"wip_limit": 1}).status_code == 409
    assert tickets(client, user_headers, project_id=1) == before
    assert client.post(
        "/tickets/4/move", headers=user_headers, json={"target_column_id": 3, "after_ticket_id": 2}
    ).status_code == 409
    assert tickets(client, user_headers, project_id=1) == before
    assert client.post(
        "/projects/1/tickets/bulk-move",
        headers=user_headers,
        json={"ticket_ids": [1, 1], "target_column_id": 3, "placement": "TOP"},
    ).status_code == 422
    assert tickets(client, user_headers, project_id=1) == before
    assert client.post("/tickets", headers={**user_headers, "Content-Type": "text/plain"}, content=b"{}").status_code == 415
    assert client.post(
        "/tickets", headers={**user_headers, "Content-Type": "application/json"}, content=b'{"project_id":'
    ).status_code == 400
    assert client.post(
        "/tickets", headers=user_headers, json={"project_id": 1, "column_id": 1, "title": "x"}
    ).status_code == 422
