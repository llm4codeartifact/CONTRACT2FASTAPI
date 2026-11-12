from conftest import assert_json, assert_status


def test_health_405_and_all_private_routes_require_authentication(client, admin_headers):
    before = assert_json(client.get("/departments", headers=admin_headers), 200)
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert_status(wrong_method, 405)
    assert "GET" in wrong_method.headers["allow"]
    private_routes = [
        ("GET", "/departments"), ("POST", "/departments"), ("GET", "/departments/1"),
        ("PATCH", "/departments/1"), ("DELETE", "/departments/1"),
        ("GET", "/departments/1/hierarchy"), ("GET", "/roles"), ("POST", "/roles"),
        ("GET", "/roles/1"), ("PATCH", "/roles/1"), ("DELETE", "/roles/1"),
        ("GET", "/assignments"), ("POST", "/assignments"), ("GET", "/assignments/1"),
        ("PATCH", "/assignments/1"), ("DELETE", "/assignments/1"),
        ("GET", "/leave-reports/by-department?year=2025"),
        ("GET", "/leave-reports/by-role?year=2025"),
        ("GET", "/employees/1/leave-report?year=2025"),
    ]
    for method, path in private_routes:
        assert_status(client.request(method, path, json={}), 401)
    assert assert_json(client.get("/departments", headers=admin_headers), 200) == before


def test_department_role_crud_hierarchy_validation_and_authorization(client, admin_headers, user_headers):
    assert_json(client.get("/departments/1", headers=user_headers), 200)
    assert_status(client.post("/departments", headers=user_headers, json={"code": "DENIED", "name": "Denied Department"}), 403)
    assert_status(client.post("/departments", headers=admin_headers, json={"code": "x", "name": "x"}), 422)
    department = assert_json(client.post("/departments", headers=admin_headers, json={"code": "QA-HTTP", "name": "HTTP Quality", "parent_department_id": 1}), 201)
    assert_status(client.post("/departments", headers=admin_headers, json={"code": "QA-HTTP", "name": "Duplicate Department"}), 409)
    patched_department = assert_json(client.patch(f"/departments/{department['id']}", headers=admin_headers, json={"name": "HTTP Quality Updated"}), 200)
    assert assert_json(client.get(f"/departments/{department['id']}", headers=admin_headers), 200) == patched_department
    assert_status(client.patch(f"/departments/{department['id']}", headers=admin_headers, json={"parent_department_id": department["id"]}), 409)
    hierarchy = assert_json(client.get("/departments/1/hierarchy", headers=admin_headers, params={"limit": 100}), 200)
    assert department["id"] in [child["id"] for child in hierarchy["children"]]
    assert_status(client.get("/departments/1/hierarchy", headers=user_headers), 403)

    assert_json(client.get("/roles/1", headers=user_headers), 200)
    assert_status(client.post("/roles", headers=user_headers, json={"code": "DENIED-ROLE", "title": "Denied Role", "annual_leave_days": 20}), 403)
    assert_status(client.post("/roles", headers=admin_headers, json={"code": "BAD", "title": "Bad", "annual_leave_days": 1.25}), 422)
    role = assert_json(client.post("/roles", headers=admin_headers, json={"code": "QA-ROLE", "title": "HTTP Quality Analyst", "annual_leave_days": 24.5}), 201)
    assert_status(client.post("/roles", headers=admin_headers, json={"code": "QA-ROLE", "title": "Duplicate Role", "annual_leave_days": 24.5}), 409)
    patched_role = assert_json(client.patch(f"/roles/{role['id']}", headers=admin_headers, json={"annual_leave_days": 25.0}), 200)
    assert patched_role["annual_leave_days"] == 25.0
    assert_status(client.patch(f"/roles/{role['id']}", headers=user_headers, json={"title": "Denied"}), 403)
    assert_status(client.post("/departments", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}"), 415)
    assert_status(client.post("/departments", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"code":'), 400)
def test_assignment_lifecycle_ownership_conflicts_deletion_and_reports(client, admin_headers, user_headers, other_user_headers):
    department = assert_json(client.post("/departments", headers=admin_headers, json={"code": "QA-ASG", "name": "Assignment Quality"}), 201)
    role = assert_json(client.post("/roles", headers=admin_headers, json={"code": "ASG-ROLE", "title": "Assignment Analyst", "annual_leave_days": 21.0}), 201)
    assert_json(client.get("/assignments/1", headers=user_headers), 200)
    assert_status(client.get("/assignments/1", headers=other_user_headers), 403)
    assert_status(client.post("/assignments", headers=user_headers, json={"employee_id": 1, "department_id": department["id"], "role_id": role["id"], "effective_from": "2026-01-01", "effective_to": "2026-12-31"}), 403)
    assignment = assert_json(client.post("/assignments", headers=admin_headers, json={"employee_id": 1, "department_id": department["id"], "role_id": role["id"], "effective_from": "2026-01-01", "effective_to": "2026-12-31"}), 201)
    assert_status(client.post("/assignments", headers=admin_headers, json={"employee_id": 1, "department_id": department["id"], "role_id": role["id"], "effective_from": "2026-06-01", "effective_to": "2026-12-31"}), 409)
    changed = assert_json(client.patch(f"/assignments/{assignment['id']}", headers=admin_headers, json={"effective_to": "2026-11-30"}), 200)
    assert assert_json(client.get(f"/assignments/{assignment['id']}", headers=user_headers), 200) == changed
    assert_status(client.patch(f"/assignments/{assignment['id']}", headers=admin_headers, json={"effective_from": "2026-12-01", "effective_to": "2026-01-01"}), 422)
    assert_status(client.delete(f"/roles/{role['id']}", headers=admin_headers), 409)
    assert_status(client.delete(f"/departments/{department['id']}", headers=admin_headers), 409)
    assert_status(client.delete(f"/assignments/{assignment['id']}", headers=admin_headers), 204)
    assert_status(client.get(f"/assignments/{assignment['id']}", headers=admin_headers), 404)
    assert_status(client.delete(f"/roles/{role['id']}", headers=admin_headers), 204)
    assert_status(client.delete(f"/departments/{department['id']}", headers=admin_headers), 204)

    assert_status(client.get("/leave-reports/by-department", headers=user_headers, params={"year": 2025}), 403)
    departments = assert_json(client.get("/leave-reports/by-department", headers=admin_headers, params={"year": 2025, "limit": 100}), 200)
    roles = assert_json(client.get("/leave-reports/by-role", headers=admin_headers, params={"year": 2025, "limit": 100}), 200)
    own_report = assert_json(client.get("/employees/1/leave-report", headers=user_headers, params={"year": 2025}), 200)
    assert own_report["total_approved_leave_days"] == 5.0
    assert_status(client.get("/employees/2/leave-report", headers=user_headers, params={"year": 2025}), 403)
    assert departments["year"] == roles["year"] == 2025
    assert_status(client.get("/assignments", headers=admin_headers, params={"department_id": 0}), 422)
    assert_status(client.get("/departments/0", headers=admin_headers), 404)
