"""Retained originals never restore formal query or saved-answer eligibility."""
from uuid import uuid4
import pytest
from sqlalchemy import select

from .models import AuthorizationGrant, Document, QueryTask, User, utc_now
from .governance_models import DocumentUseHold
from .services import build_query_result
from .test_cached_result_access import saved_query
from .test_services import fixture_db


@pytest.mark.parametrize("status", ["superseded", "withdrawn"])
def test_human_historical_original_is_separate_from_formal_and_saved_results(saved_query, status):
    db, task, version, evidence, _, _, owner, recipient = saved_query
    version.publish_status = status; db.commit()
    path = f"/documents/{version.document_id}/versions/{version.id}/file"
    response = recipient.get(path)
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    assert response.headers["X-Document-Read-Mode"] == "historical"
    assert response.headers["X-Document-Publication-State"] == status
    assert response.headers["X-Formal-Use-Requires-Validation"] == "true"
    assert "no-store" in response.headers["Cache-Control"]
    versions = recipient.get(f"/documents/{version.document_id}/versions").json()["items"]
    row = next(item for item in versions if item["id"] == version.id)
    assert row["read_mode"] == "historical" and row["formal_use_requires_current_validation"]
    assert recipient.get(f"/tasks/{task.id}/result").status_code == (200 if status == "superseded" else 404)
    fresh = QueryTask(owner_user_id=task.owner_user_id, kind="query", status="running",
        idempotency_key=uuid4().hex, input_snapshot=dict(task.input_snapshot))
    db.add(fresh); db.flush()
    build_query_result(db, fresh, db.get(User, task.owner_user_id)); db.commit()
    assert fresh.result_summary["result_status"] == "insufficient_evidence"
    document = db.get(Document, version.document_id)
    document.deleted_at = utc_now(); db.commit()
    assert recipient.get(path).status_code == 404 and owner.get(path).status_code == 404


def test_held_original_can_be_reviewed_but_never_used_as_formal_answer(saved_query):
    db, task, version, evidence, _, _, owner, recipient = saved_query
    hold = DocumentUseHold(document_version_id=version.id, kind="quality", reason="SYNTHETIC quality check",
        created_by=task.owner_user_id)
    db.add(hold); db.commit()
    path = f"/documents/{version.document_id}/versions/{version.id}/file"
    assert recipient.get(path).status_code == 200
    assert recipient.get(f"/tasks/{task.id}/result").status_code == 404
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.operation == "document.read",
        AuthorizationGrant.subject_user_id != task.owner_user_id))
    grant.revoked_at = utc_now(); db.commit()
    assert recipient.get(path).status_code == 404
