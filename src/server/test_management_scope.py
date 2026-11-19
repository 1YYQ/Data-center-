"""A KB grant must not authorize management of a different document class."""
import pytest
import json
from uuid import uuid4
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from .app import create_app
from .governance import manage
from .services import SafeError, can_manage_kb
from .test_services import fixture_db, _ready_fixture
from sqlalchemy import select
from .models import AuthorizationGrant, BusinessApproval, User
from .governance import approval_is_current, register_approval


def test_management_requires_the_document_complete_tuple(fixture_db):
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    document.access_level = "SYNTHETIC-OUTSIDE-GRANT"
    db.commit()
    assert can_manage_kb(db, actor, kb.id)
    with pytest.raises(SafeError) as error:
        manage(db, actor, document)
    assert error.value.status_code == 403
    app = create_app(config)
    with TestClient(app) as client:
        login = client.post("/auth/login", json={"username": actor.username, "password": "manager password"})
        assert login.status_code == 200
        result = client.get(f"/parse-runs/{block.parse_run_id}/blocks")
        assert result.status_code in (403, 404)
        assert block.text_content not in result.text
        context = client.get(f"/documents/{document.id}/versions/{version.id}/confirmation-options")
        assert context.status_code in (403, 404)
        source = client.get(f"/documents/{document.id}/versions/{version.id}/source-metadata")
        assert source.status_code in (403, 404)
    app.state.engine.dispose()


def test_source_metadata_read_is_exact_version_and_current_management_scope(fixture_db):
    from .models import DocumentVersion, utc_now
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    draft = DocumentVersion(document_id=document.id, version_no=version.version_no + 1,
                            source_file_id=version.source_file_id, created_by=actor.id,
                            source_metadata={"channel": "synthetic", "source_description": "SYNTHETIC exact draft source"})
    db.add(draft); db.commit()
    app = create_app(config)
    try:
        with TestClient(app) as client:
            assert client.post("/auth/login", json={"username": actor.username, "password": "manager password"}).status_code == 200
            path = f"/documents/{document.id}/versions/{draft.id}/source-metadata"
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["ETag"] == response.json()["etag"] == f'"{draft.revision}"'
            assert response.json()["source_metadata"] == draft.source_metadata
            assert "no-store" in response.headers["Cache-Control"]
            grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == actor.id,
                AuthorizationGrant.operation == "knowledge.manage"))
            grant.revoked_at = utc_now(); db.commit()
            denied = client.get(path)
            assert denied.status_code in (403, 404)
            assert "SYNTHETIC exact draft source" not in denied.text
    finally:
        app.state.engine.dispose()


def test_out_of_scope_approval_supersession_cannot_invalidate_existing_grant(fixture_db):
    db, config = fixture_db
    owner, kb, document, version, block = _ready_fixture(db, config)
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == owner.id))
    old = db.get(BusinessApproval, grant.approval_id)
    assert approval_is_current(db, old)

    administrator = User(username="SYNTHETIC-unappointed-admin", display_name="Synthetic admin",
                         status="active", roles=["system_admin"])
    db.add(administrator)
    db.commit()
    with pytest.raises(SafeError) as error:
        register_approval(db, administrator, {"applicant": owner.id, "scope": dict(old.scope),
            "operations": list(old.operations), "supersedes_id": old.id})
    assert error.value.status_code == 403
    db.rollback()
    assert approval_is_current(db, old)


def test_linked_uploaded_confirmation_material_does_not_attest_or_confirm(fixture_db):
    from .test_worker import synthetic_pdf
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    app = create_app(config)
    with TestClient(app) as client:
        login = client.post("/auth/login", json={"username": actor.username, "password": "manager password"})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        base = f"/documents/{document.id}/versions/{version.id}"
        context = client.get(base + "/confirmation-options").json()
        registered = client.post(base + "/business-confirmations", json={
            "kind": "applicability", "conclusion": "applicable", "scope": context["source_binding"],
            "actual_confirmer": {"identity_label": "SYNTHETIC owner", "responsibility": "SYNTHETIC applicability",
                "authority_basis_ref": "SYNTHETIC-unregistered-root"},
            "confirmed_at": datetime.now(timezone.utc).isoformat(), "proof_refs": []},
            headers={"Idempotency-Key": str(uuid4())})
        assert registered.status_code == 201, registered.text
        record = registered.json()
        uploaded = client.post(base + "/confirmation-evidence",
            files={"file": ("SYNTHETIC-proof.pdf", synthetic_pdf(), "application/pdf")},
            data={"metadata": json.dumps({"source_description": "SYNTHETIC uploaded unverified material"})},
            headers={"Idempotency-Key": str(uuid4())})
        assert uploaded.status_code == 201, uploaded.text
        ref = uploaded.json()["id"]
        path = base + f"/business-confirmations/{record['id']}"
        headers = {"Idempotency-Key": str(uuid4()), "If-Match": record["etag"]}
        linked = client.patch(path + "/evidence-refs", json={"proof_refs": [ref]}, headers=headers)
        assert linked.status_code == 200, linked.text
        assert client.patch(path + "/evidence-refs", json={"proof_refs": [ref]}, headers=headers).json() == linked.json()
        options = client.get(path + "/verification-options").json()
        assert next(x for x in options["evidence"] if x["id"] == ref)["attested"] is False
        denied = client.post(path + "/verification", json={"decision": "verified", "reason": "SYNTHETIC cannot self attest",
            "checked_proof_refs": [ref], "authority_basis_ref": "SYNTHETIC-unregistered-root"},
            headers={"Idempotency-Key": str(uuid4()), "If-Match": linked.json()["etag"]})
        assert denied.status_code >= 400
        assert client.get(path).json()["current"] is False
    app.state.engine.dispose()
