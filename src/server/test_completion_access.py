"""Actual scoped read APIs on an isolated SQLite fixture, no model calls."""
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from .app import create_app
from .governance_models import AuthorityBasis
from .models import AuthorizationGrant, BusinessApproval, KnowledgeBase, User, utc_now
from .security import hash_password
from .test_services import fixture_db
from .testing_governance import seed_verified_approval


def test_grant_listing_is_scoped_and_query_choices_require_actual_query_grants(fixture_db):
    db, config = fixture_db
    now = utc_now()
    password = "SYNTHETIC completion test password"
    administrator = User(username="synthetic-admin-" + uuid4().hex, display_name="Synthetic admin", roles=["system_admin"],
        status="active", credential_state="active", credential_hash=hash_password(password))
    employee = User(username="synthetic-employee-" + uuid4().hex, display_name="Synthetic employee", roles=["employee"],
        status="active", credential_state="active", credential_hash=hash_password(password))
    kb = KnowledgeBase(name="SYNTHETIC available query library" + uuid4().hex)
    hidden = KnowledgeBase(name="SYNTHETIC unauthorized library" + uuid4().hex)
    db.add_all([administrator, employee, kb, hidden]); db.flush()
    scope = {"knowledge_base_id": kb.id, "document_id": None, "level":"internal", "category":"manual", "need_class":"maintenance"}
    db.add(AuthorityBasis(identity={"user_ref":administrator.id,"identity_label":"Synthetic admin","responsibility":"Synthetic access administration"},
        purposes=["access.manage"], scope={"knowledge_base_id":kb.id}, source_reference="synthetic://completion-test",
        valid_from=now-timedelta(days=1), valid_until=now+timedelta(days=2)))
    approval = BusinessApproval(applicant=employee.id, recorded_by=administrator.id, scope=scope,
        operations=["knowledge.query"], valid_until=now+timedelta(days=1))
    db.add(approval); db.flush(); seed_verified_approval(db,approval)
    active = AuthorizationGrant(subject_user_id=employee.id, operation="knowledge.query", **scope,
        approval_id=approval.id, granted_by=administrator.id, valid_until=approval.valid_until)
    expired = AuthorizationGrant(subject_user_id=employee.id, operation="knowledge.query", **scope,
        approval_id=approval.id, granted_by=administrator.id, valid_from=now-timedelta(hours=1), valid_until=now-timedelta(seconds=1))
    db.add_all([active,expired]); db.commit()
    app = create_app(config)
    try:
        with TestClient(app) as client:
            assert client.post("/auth/login",json={"username":administrator.username,"password":password}).status_code == 200
            response = client.get("/admin/grants",params={"user_id":employee.id})
            assert response.status_code == 200
            records = {row["id"]:row for row in response.json()["items"]}
            assert records[active.id]["effective"] is True
            assert records[expired.id]["effective"] is False
            assert client.get("/knowledge/query-scopes").status_code == 403
            assert client.post("/auth/login",json={"username":employee.username,"password":password}).status_code == 200
            assert client.get("/admin/grants").status_code == 403
            choices = client.get("/knowledge/query-scopes")
            assert choices.status_code == 200
            assert {row["id"] for row in choices.json()["items"]} == {kb.id}
            assert hidden.name not in choices.text
            active.revoked_at=utc_now(); db.commit()
            assert client.get("/knowledge/query-scopes").json()["items"] == []
    finally:
        app.state.engine.dispose()
