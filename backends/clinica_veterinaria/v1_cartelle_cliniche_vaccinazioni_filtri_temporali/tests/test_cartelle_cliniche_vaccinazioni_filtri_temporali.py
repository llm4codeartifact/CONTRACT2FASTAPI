from conftest import assert_json, assert_status


def _items(client, path, headers, **params):
    body = assert_json(client.get(path, headers=headers, params=params), 200)
    assert {"items", "limit", "next_after_id"} == set(body)
    assert [item["id"] for item in body["items"]] == sorted(item["id"] for item in body["items"])
    return body


def test_health_method_and_private_routes_require_authentication(client, admin_headers):
    before = _items(client, "/animals", admin_headers, limit=100)["items"]
    assert assert_json(client.get("/health"), 200) == {"status": "ok", "database": "connected"}
    wrong_method = client.post("/health")
    assert_status(wrong_method, 405)
    assert "GET" in wrong_method.headers["allow"].upper()
    private_requests = [
        ("GET", "/animals"), ("POST", "/animals"), ("GET", "/animals/1"), ("PATCH", "/animals/1"), ("DELETE", "/animals/1"),
        ("GET", "/clinical-records"), ("POST", "/animals/1/clinical-records"), ("GET", "/clinical-records/1"),
        ("PATCH", "/clinical-records/1"), ("POST", "/clinical-records/1/finalize"), ("DELETE", "/clinical-records/1"),
        ("GET", "/vaccinations"), ("POST", "/animals/1/vaccinations"), ("GET", "/vaccinations/1"),
        ("POST", "/vaccinations/1/void"), ("GET", "/animals/1/vaccination-status?as_of=2025-06-01"),
        ("GET", "/animals/1/clinical-timeline"),
    ]
    for method, path in private_requests:
        assert_status(client.request(method, path, json={}), 401)
    assert _items(client, "/animals", admin_headers, limit=100)["items"] == before


def test_animal_crud_ownership_and_deletion_constraints(client, admin_headers, user_headers, other_user_headers):
    created_response = client.post("/animals", headers=user_headers, json={"name": "Black Box Temporary Animal", "species": "RABBIT", "active": True})
    animal = assert_json(created_response, 201)
    assert created_response.headers["location"].endswith(f"/animals/{animal['id']}")
    assert animal["client_account_id"] == 1
    assert_status(client.post("/animals", headers=user_headers, json={"name": "Foreign animal", "species": "CAT", "client_account_id": 2}), 403)
    assert_status(client.get("/animals", headers=user_headers, params={"client_account_id": 1}), 403)
    assert_status(client.get(f"/animals/{animal['id']}", headers=other_user_headers), 403)
    patched = assert_json(client.patch(f"/animals/{animal['id']}", headers=user_headers, json={"name": "Black Box Temporary Animal Patched", "active": False}), 200)
    assert patched["name"] == "Black Box Temporary Animal Patched" and not patched["active"]
    assert_status(client.patch(f"/animals/{animal['id']}", headers=user_headers, json={}), 400)
    assert_status(client.patch(f"/animals/{animal['id']}", headers=other_user_headers, json={"active": True}), 403)
    assert_status(client.delete(f"/animals/{animal['id']}", headers=user_headers), 204)
    assert_status(client.get(f"/animals/{animal['id']}", headers=admin_headers), 404)
    assert_status(client.delete("/animals/1", headers=user_headers), 409)
    assert_status(client.post("/animals", headers={**admin_headers, "Content-Type": "text/plain"}, content=b"{}"), 415)
    assert_status(client.post("/animals", headers={**admin_headers, "Content-Type": "application/json"}, content=b'{"name":'), 400)
    assert_status(client.post("/animals", headers=admin_headers, json={"name": "", "species": "NOPE"}), 422)


def test_clinical_record_draft_final_history_and_filters(client, admin_headers, user_headers, other_user_headers):
    assert_status(client.post("/animals/1/clinical-records", headers=user_headers, json={"record_type": "VISIT", "occurred_on": "2030-01-01", "summary": "Denied"}), 403)
    response = client.post("/animals/1/clinical-records", headers=admin_headers, json={"record_type": "LAB_NOTE", "occurred_on": "2030-01-10", "summary": "Black box clinical record", "diagnosis_code": "LAB-10"})
    record = assert_json(response, 201)
    assert response.headers["location"].endswith(f"/clinical-records/{record['id']}")
    assert record["status"] == "DRAFT" and record["animal_id"] == 1
    assert_json(client.get(f"/clinical-records/{record['id']}", headers=user_headers), 200)
    assert_status(client.get(f"/clinical-records/{record['id']}", headers=other_user_headers), 403)
    assert_status(client.patch(f"/clinical-records/{record['id']}", headers=user_headers, json={"summary": "Denied"}), 403)
    patched = assert_json(client.patch(f"/clinical-records/{record['id']}", headers=admin_headers, json={"summary": "Black box clinical record amended"}), 200)
    assert patched["summary"] == "Black box clinical record amended"
    assert_status(client.patch(f"/clinical-records/{record['id']}", headers=admin_headers, json={}), 400)
    finalized = assert_json(client.post(f"/clinical-records/{record['id']}/finalize", headers=admin_headers, json={"finalized_at": "2030-01-10T12:00:00Z"}), 200)
    assert finalized["status"] == "FINAL" and finalized["finalized_by"] == 3
    assert_status(client.post(f"/clinical-records/{record['id']}/finalize", headers=admin_headers, json={"finalized_at": "2030-01-10T12:00:00Z"}), 409)
    assert_status(client.patch(f"/clinical-records/{record['id']}", headers=admin_headers, json={"summary": "Forbidden after final"}), 409)
    assert_status(client.delete(f"/clinical-records/{record['id']}", headers=admin_headers), 409)
    page = _items(client, "/clinical-records", admin_headers, animal_id=1, occurred_from="2030-01-10", occurred_to="2030-01-10", limit=100)
    assert record["id"] in [item["id"] for item in page["items"]]
    assert_status(client.get("/clinical-records", headers=admin_headers, params={"occurred_from": "2030-02-01", "occurred_to": "2030-01-01"}), 400)
    assert_status(client.get("/clinical-records", headers=admin_headers, params={"diagnosis_code_prefix": "lower"}), 400)

    disposable = assert_json(client.post("/animals/1/clinical-records", headers=admin_headers, json={"record_type": "VISIT", "occurred_on": "2030-01-11", "summary": "Disposable draft"}), 201)
    assert_status(client.delete(f"/clinical-records/{disposable['id']}", headers=admin_headers), 204)
    assert_status(client.get(f"/clinical-records/{disposable['id']}", headers=admin_headers), 404)


def test_vaccination_void_status_timeline_and_persistence(client, admin_headers, user_headers, other_user_headers):
    rabies = assert_json(client.get("/animals/1/vaccination-status", headers=user_headers, params={"as_of": "2025-06-01", "vaccine_code": "RABIES"}), 200)
    assert rabies["statuses"] == [{"vaccine_code": "RABIES", "status": "CURRENT", "administered_on": "2025-01-15", "expires_on": "2026-01-15"}]
    assert_status(client.get("/animals/1/vaccination-status", headers=other_user_headers, params={"as_of": "2025-06-01"}), 403)
    created_response = client.post("/animals/1/vaccinations", headers=admin_headers, json={"vaccine_code": "FELV", "vaccine_name": "Black Box Felv", "administered_on": "2030-02-01", "expires_on": "2031-02-01", "batch_number": "BB-FELV-1"})
    vaccination = assert_json(created_response, 201)
    assert created_response.headers["location"].endswith(f"/vaccinations/{vaccination['id']}")
    assert_status(client.post("/animals/1/vaccinations", headers=user_headers, json={"vaccine_code": "FELV", "vaccine_name": "Denied", "administered_on": "2030-02-01", "expires_on": "2031-02-01", "batch_number": "DENIED"}), 403)
    assert_status(client.post("/animals/1/vaccinations", headers=admin_headers, json={"vaccine_code": "FELV", "vaccine_name": "Duplicate", "administered_on": "2030-02-01", "expires_on": "2031-02-01", "batch_number": "BB-FELV-2"}), 409)
    assert_status(client.post("/animals/1/vaccinations", headers=admin_headers, json={"vaccine_code": "OTHER", "vaccine_name": "Invalid", "administered_on": "2030-03-01", "expires_on": "2030-03-01", "batch_number": "INVALID"}), 422)
    assert_json(client.get(f"/vaccinations/{vaccination['id']}", headers=user_headers), 200)
    assert_status(client.get("/vaccinations", headers=user_headers, params={"include_void": True}), 403)
    voided = assert_json(client.post(f"/vaccinations/{vaccination['id']}/void", headers=admin_headers, json={"voided_on": "2030-02-02", "void_reason": "Black box correction"}), 200)
    assert voided["is_void"] and voided["voided_by"] == 3 and voided["voided_on"] == "2030-02-02"
    assert_status(client.post(f"/vaccinations/{vaccination['id']}/void", headers=admin_headers, json={"voided_on": "2030-02-03", "void_reason": "Repeated void"}), 409)
    all_vaccinations = _items(client, "/vaccinations", admin_headers, animal_id=1, include_void=True, limit=100)
    assert vaccination["id"] in [item["id"] for item in all_vaccinations["items"]]
    assert_status(client.get("/vaccinations", headers=admin_headers, params={"administered_from": "2031-01-01", "administered_to": "2030-01-01"}), 400)
    timeline = assert_json(client.get("/animals/1/clinical-timeline", headers=admin_headers, params={"include_void": True, "limit": 1}), 200)
    assert len(timeline["items"]) == 1 and timeline["next_after_event_id"] is not None
    next_page = assert_json(client.get("/animals/1/clinical-timeline", headers=admin_headers, params={"include_void": True, "after_event_date": timeline["next_after_event_date"], "after_event_kind": timeline["next_after_event_kind"], "after_event_id": timeline["next_after_event_id"], "limit": 100}), 200)
    assert next_page["items"]
    assert_status(client.get("/animals/1/clinical-timeline", headers=user_headers, params={"include_void": True}), 403)
    assert_status(client.get("/animals/1/clinical-timeline", headers=admin_headers, params={"after_event_id": 1}), 400)
