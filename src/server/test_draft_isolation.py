"""Current management scope for incompletely classified material in a synthetic KB."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from .app import create_app
from .models import AuthorizationGrant, BusinessApproval, Document, DocumentVersion, KnowledgeBase, User, utc_now
from .security import hash_password
from .services import can_access_document
from .test_creation import metadata, upload
from .test_services import fixture_db
from .test_worker import synthetic_pdf
from .testing_governance import seed_verified_approval


def _user(db, username, role):
    actor = User(username=username, display_name=username, status="active", roles=[role],
        credential_state="active", credential_hash=hash_password("synthetic draft password"))
    db.add(actor)
    db.flush()
    return actor


def _grant(db, actor, kb, operation="knowledge.manage", *, document_id=None, category="test-category"):
    scope = {"knowledge_base_id": kb.id, "document_id": document_id, "level": "test-level",
             "category": category, "need_class": "test-need"}
    approval = BusinessApproval(applicant=actor.id, scope=scope, operations=[operation])
    db.add(approval)
    seed_verified_approval(db, approval)
    grant = AuthorizationGrant(subject_user_id=actor.id, operation=operation, approval_id=approval.id,
        valid_until=approval.valid_until, **scope)
    db.add(grant)
    return grant


def _login(client, actor):
    response = client.post("/auth/login", json={"username": actor.username, "password": "synthetic draft password"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


@pytest.fixture
def draft(fixture_db):
    db, config = fixture_db
    owner = _user(db, "synthetic-draft-uploader", "knowledge_manager")
    peer = _user(db, "synthetic-draft-peer", "knowledge_manager")
    kb = KnowledgeBase(name="SYNTHETIC draft isolation KB")
    db.add(kb)
    db.flush()
    owner_grant = _grant(db, owner, kb)
    _grant(db, peer, kb)
    db.commit()

    app = create_app(config)
    try:
        with TestClient(app) as client:
            _login(client, owner)
            response = upload(client, kb, metadata())
            assert response.status_code == 201, response.text
            document = db.get(Document, response.json()["document"]["id"])
            version = db.get(DocumentVersion, response.json()["version"]["id"])
            assert document.access_level is document.need_class is None
            assert version.version_no == 1 and version.created_by == owner.id
            yield db, client, kb, owner, peer, document, version, owner_grant
    finally:
        app.state.engine.dispose()


def test_peer_with_same_kb_scope_cannot_read_or_write_private_draft(draft):
    db, client, _, _, peer, document, version, _ = draft
    version_path = f"/documents/{document.id}/versions/{version.id}"
    own_file = client.get(version_path + "/file")
    assert own_file.status_code == 200 and own_file.content == synthetic_pdf()
    _login(client, peer)
    peer_file = client.get(version_path + "/file")
    patch = client.patch(version_path, json={"revision_label": "SYNTHETIC peer edit"},
        headers={"If-Match": f'"{version.revision}"', "Idempotency-Key": str(uuid4())})
    assert (peer_file.status_code, patch.status_code) in {(404, 403), (404, 404)}
    assert document.title not in peer_file.text
    db.refresh(version)
    assert version.revision_label == "SYNTHETIC-1"


def test_private_draft_is_filtered_from_peer_catalog_and_lifecycle(draft):
    _, client, kb, _, peer, document, _, _ = draft
    doc_path = f"/documents/{document.id}"
    lifecycle = f"/knowledge-bases/{kb.id}/document-lifecycle?state=all"
    catalog = f"/knowledge-bases/{kb.id}/documents"
    assert client.get(doc_path + "/lifecycle").status_code == 200
    _login(client, peer)
    listed = [client.get(path) for path in (catalog, lifecycle)]
    details = [client.get(doc_path + suffix) for suffix in ("", "/lifecycle", "/deletion-impact")]
    assert all(result.status_code == 200 and result.json()["items"] == [] for result in listed)
    assert all(result.status_code == 404 for result in details)
    assert all(document.title not in result.text for result in listed + details)


def test_uploader_cannot_keep_reading_after_management_grant_revocation(draft):
    db, client, _, _, _, document, version, owner_grant = draft
    path = f"/documents/{document.id}/versions/{version.id}/file"
    assert client.get(path).status_code == 200
    owner_grant.revoked_at = utc_now()
    db.commit()
    assert client.get(path).status_code == 404
    assert client.get(f"/documents/{document.id}/lifecycle").status_code == 404


def test_exact_document_grant_can_allow_peer_until_its_approval_is_invalid(draft):
    db, client, kb, _, peer, document, version, _ = draft
    _grant(db, peer, kb, document_id=document.id, category="outside-category")
    db.commit()
    assert not can_access_document(db, peer, document, "knowledge.manage")
    grant = _grant(db, peer, kb, document_id=document.id)
    db.commit()
    _login(client, peer)
    path = f"/documents/{document.id}/versions/{version.id}"
    result = client.get(path + "/source-metadata")
    assert result.status_code == 200 and result.json()["source_metadata"] == version.source_metadata
    assert client.get(path + "/file").content == synthetic_pdf()
    approval = db.get(BusinessApproval, grant.approval_id)
    approval.verification_state = "unverified"
    db.commit()
    assert client.get(path + "/file").status_code == 404
    assert client.get(f"/documents/{document.id}/lifecycle").status_code == 404


def test_complete_classification_uses_the_existing_matching_tuple(draft):
    db, client, _, owner, peer, document, _, _ = draft
    classification = {"access_level": "test-level", "category": "test-category", "need_class": "test-need"}
    for field in classification:
        for key, value in classification.items():
            setattr(document, key, None if key == field else value)
        db.commit()
        assert can_access_document(db, owner, document, "knowledge.manage"), field
        assert not can_access_document(db, peer, document, "knowledge.manage"), field

    for key, value in classification.items():
        setattr(document, key, value)
    db.commit()
    _login(client, peer)
    path = f"/documents/{document.id}/lifecycle"
    assert client.get(path).status_code == 200
    document.need_class = "outside-need"
    db.commit()
    assert client.get(path).status_code == 404


def test_missing_first_version_does_not_infer_owner_from_later_upload(draft):
    db, _, kb, owner, _, document, version, _ = draft
    version.version_no = 2  # Model a legacy document without a recorded first version.
    db.commit()
    assert not can_access_document(db, owner, document, "knowledge.manage")
    _grant(db, owner, kb, document_id=document.id)
    db.commit()
    assert can_access_document(db, owner, document, "knowledge.manage")


def test_incomplete_classification_does_not_extend_employee_or_system_admin_rights(draft):
    db, client, kb, _, _, document, version, _ = draft
    employee = _user(db, "synthetic-draft-employee", "employee")
    _grant(db, employee, kb, "document.read", document_id=document.id)
    _grant(db, employee, kb, "knowledge.query", document_id=document.id)
    admin = _user(db, "synthetic-draft-admin", "system_admin")
    _grant(db, admin, kb, document_id=document.id)
    version.reference_access_state = "open"  # Incomplete classification must block even a stale open flag.
    db.commit()
    path = f"/documents/{document.id}/versions/{version.id}/file"
    assert not can_access_document(db, employee, document)
    assert not can_access_document(db, employee, document, "knowledge.query")
    assert not can_access_document(db, admin, document, "knowledge.manage")
    for actor in (employee, admin):
        _login(client, actor)
        assert client.get(path).status_code == 404
