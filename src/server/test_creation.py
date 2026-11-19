"""Local creation must preserve approval, classification and receipt boundaries."""
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from .app import create_app
from .models import AuthorizationGrant, BusinessApproval, Document, DocumentVersion, FileObject, KnowledgeBase, User, utc_now
from .security import hash_password
from .services import document_creation_scopes
from .test_services import fixture_db, _ready_fixture
from .test_worker import synthetic_pdf


@pytest.fixture
def creation_http(fixture_db):
    db, config = fixture_db
    manager, kb, document, version, block = _ready_fixture(db, config)
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post("/auth/login", json={"username": manager.username, "password": "manager password"})
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        yield db, config, client, manager, kb, document, version
    app.state.engine.dispose()


def metadata(category="test-category"):
    return {"title": "SYNTHETIC new manual", "category": category, "model_scope": ["DEMO-1"],
            "revision_label": "SYNTHETIC-1", "source_metadata": {"channel": "synthetic-local",
                "source_description": "SYNTHETIC controlled source"}}


def upload(client, kb, details, key=None, data=None):
    return client.post(f"/knowledge-bases/{kb.id}/documents", files={"file": ("SYNTHETIC.pdf", data or synthetic_pdf(), "application/pdf")},
        data={"metadata": json.dumps(details)}, headers={"Idempotency-Key": key or str(uuid4())})


def test_new_kb_registration_never_creates_business_grants(creation_http):
    db, config, client, manager, kb, document, version = creation_http
    before = db.scalar(select(func.count()).select_from(AuthorizationGrant))
    headers = {"Idempotency-Key": str(uuid4())}
    response = client.post("/admin/knowledge-bases", json={"name": "SYNTHETIC KB " + uuid4().hex}, headers=headers)
    assert response.status_code == 201
    assert client.post("/admin/knowledge-bases", json={"name": response.json()["name"]}, headers=headers).json() == response.json()
    identity = response.json()["id"]
    rows = client.get("/admin/knowledge-bases/registrations").json()["items"]
    assert next(row for row in rows if row["id"] == identity)["access_state"] == "awaiting_authorization"
    assert identity not in {row["id"] for row in client.get("/knowledge-bases").json()["items"]}
    assert client.get(f"/knowledge-bases/{identity}/document-creation-options").status_code == 404
    db.expire_all(); assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == before


def test_new_document_metadata_and_replay_do_not_classify_or_publish(creation_http):
    db, config, client, manager, kb, document, version = creation_http
    options = client.get(f"/knowledge-bases/{kb.id}/document-creation-options")
    assert options.status_code == 200 and options.json()["can_create"] is True
    before = db.scalar(select(func.count()).select_from(AuthorizationGrant))
    key, details = str(uuid4()), metadata()
    first = upload(client, kb, details, key)
    assert first.status_code == 201, first.text
    second = upload(client, kb, details, key)
    assert second.status_code == 201 and second.json() == first.json()
    row = db.get(Document, first.json()["document"]["id"])
    new = db.get(DocumentVersion, first.json()["version"]["id"])
    assert row.access_level is row.need_class is None and row.access_mode == "inherit"
    assert new.publish_status == "draft" and new.applicability_status == "pending"
    assert new.reference_access_state == "closed" and new.source_metadata == details["source_metadata"]
    assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == before
    changed = upload(client, kb, {**details, "title": "SYNTHETIC changed input"}, key)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_single_document_manager_cannot_create_other_documents(creation_http):
    from .testing_governance import seed_verified_approval
    db, config, client, manager, kb, document, version = creation_http
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == manager.id,
        AuthorizationGrant.operation == "knowledge.manage"))
    grant.document_id = document.id
    approval = db.get(BusinessApproval, grant.approval_id)
    approval.scope = {**approval.scope, "document_id": document.id}
    seed_verified_approval(db, approval); db.commit()
    assert document_creation_scopes(db, manager, kb.id) == []
    login = client.post("/auth/login", json={"username": manager.username, "password": "manager password"})
    assert login.status_code == 200
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    options = client.get(f"/knowledge-bases/{kb.id}/document-creation-options")
    assert options.status_code == 200 and options.json()["can_create"] is False
    before = db.scalar(select(func.count()).select_from(Document))
    response = upload(client, kb, metadata())
    assert response.status_code == 403
    assert db.scalar(select(func.count()).select_from(Document)) == before


@pytest.mark.parametrize("changes", [{"access_mode": "inherit"}, {"grants": []}, {"source_metadata": {"channel": "synthetic", "approved": True}}])
def test_upload_metadata_does_not_accept_acl_or_confirmed_fields(creation_http, changes):
    db, config, client, manager, kb, document, version = creation_http
    before = db.scalar(select(func.count()).select_from(Document))
    response = upload(client, kb, {**metadata(), **changes})
    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Document)) == before


def test_unapproved_category_and_non_pdf_do_not_create_visible_document(creation_http):
    db, config, client, manager, kb, document, version = creation_http
    before = db.scalar(select(func.count()).select_from(Document))
    assert upload(client, kb, metadata("SYNTHETIC-outside-management")).status_code == 403
    assert upload(client, kb, metadata(), data=b"SYNTHETIC not a PDF").status_code == 415
    assert db.scalar(select(func.count()).select_from(Document)) == before


def test_employee_cannot_create_or_inspect_other_users_registrations(creation_http):
    db, config, client, manager, kb, document, version = creation_http
    employee = User(username="synthetic-employee-" + uuid4().hex, display_name="SYNTHETIC employee", status="active",
        credential_state="active", credential_hash=hash_password("synthetic employee password"), roles=["employee"])
    db.add(employee); db.commit()
    login = client.post("/auth/login", json={"username": employee.username, "password": "synthetic employee password"})
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    assert client.post("/admin/knowledge-bases", json={"name": "SYNTHETIC forbidden"}, headers={"Idempotency-Key": str(uuid4())}).status_code == 403
    assert client.get("/admin/knowledge-bases/registrations").status_code == 403
    assert upload(client, kb, metadata()).status_code == 403


@pytest.mark.parametrize("kind", ["document", "version"])
def test_upload_expired_at_final_validation_rolls_back_business_and_receipt(creation_http, monkeypatch, kind):
    import importlib
    from datetime import timedelta
    from . import services
    from .models import ApiIdempotencyRecord
    db, config, client, manager, kb, document, version = creation_http
    module = importlib.import_module(create_app.__module__)
    real_save, real_now = module.save_upload, services.utc_now
    before = {model: db.scalar(select(func.count()).select_from(model))
              for model in (Document, DocumentVersion, FileObject, ApiIdempotencyRecord)}
    def expire_after_save(*args, **kwargs):
        result = real_save(*args, **kwargs)
        expired = real_now() + timedelta(seconds=config.session_idle_seconds + 2)
        monkeypatch.setattr(services, "utc_now", lambda: expired)
        return result
    monkeypatch.setattr(module, "save_upload", expire_after_save)
    key = str(uuid4())
    if kind == "document":
        response = upload(client, kb, metadata(), key)
    else:
        response = client.post(f"/documents/{document.id}/versions", files={"file": ("SYNTHETIC.pdf", synthetic_pdf(), "application/pdf")},
            headers={"Idempotency-Key": key})
    assert response.status_code == 401, response.text
    db.expire_all()
    assert {model: db.scalar(select(func.count()).select_from(model)) for model in before} == before
    monkeypatch.setattr(services, "utc_now", real_now)
    monkeypatch.setattr(module, "save_upload", real_save)
    if kind == "document":
        retried = upload(client, kb, metadata(), key)
    else:
        retried = client.post(f"/documents/{document.id}/versions", files={"file": ("SYNTHETIC.pdf", synthetic_pdf(), "application/pdf")},
            headers={"Idempotency-Key": key})
    assert retried.status_code == 201, retried.text
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ApiIdempotencyRecord)) == before[ApiIdempotencyRecord] + 1


def test_creation_category_is_rechecked_after_file_install(creation_http, monkeypatch):
    import importlib
    from . import services
    db, config, client, manager, kb, document, version = creation_http
    module = importlib.import_module(create_app.__module__)
    real_save, real_scopes = module.save_upload, services.document_creation_scopes
    before = db.scalar(select(func.count()).select_from(Document))
    def change_projection_after_save(*args, **kwargs):
        result = real_save(*args, **kwargs)
        # Control the external authorization projection changing at this boundary:
        # some KB scope remains, but none matches this requested category.
        scopes = real_scopes(db, manager, kb.id)
        monkeypatch.setattr(services, "document_creation_scopes", lambda *a: [{**s, "category": "SYNTHETIC-other"} for s in scopes])
        return result
    monkeypatch.setattr(module, "save_upload", change_projection_after_save)
    response = upload(client, kb, metadata())
    assert response.status_code == 403
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Document)) == before


@pytest.mark.parametrize("change", ["deleted", "category"])
def test_new_document_replay_rechecks_current_document(creation_http, change):
    from .models import ApiIdempotencyRecord
    db, config, client, manager, kb, document, version = creation_http
    key = str(uuid4())
    first = upload(client, kb, metadata(), key)
    assert first.status_code == 201
    created = db.get(Document, first.json()["document"]["id"])
    if change == "deleted":
        created.deleted_at = utc_now()
    else:
        created.category = "SYNTHETIC-other-management-scope"
    db.commit()
    before = {model: db.scalar(select(func.count()).select_from(model))
              for model in (Document, DocumentVersion, FileObject, ApiIdempotencyRecord)}
    replay = upload(client, kb, metadata(), key)
    assert replay.status_code == 404, replay.text
    db.expire_all()
    assert {model: db.scalar(select(func.count()).select_from(model)) for model in before} == before
