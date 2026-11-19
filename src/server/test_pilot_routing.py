"""The ordinary query page follows only the trusted deployment default."""
from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from .app import create_app
from .models import QueryTask
from .test_services import fixture_db, _ready_fixture


def test_deployment_selected_service_has_no_silent_local_fallback_and_explicit_mode_survives(fixture_db):
    db, settings = fixture_db
    actor, kb, *_ = _ready_fixture(db, settings)
    app = create_app(replace(settings, query_service_mode="selected_service"))
    try:
        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": actor.username, "password": "manager password"})
            assert login.status_code == 200
            headers = {"X-CSRF-Token": login.json()["csrf_token"], "Idempotency-Key": str(uuid4())}
            body = {"query": "SYNTHETIC startup", "model": "DEMO-1", "knowledge_base_ids": [kb.id]}
            denied = client.post("/knowledge/queries", json=body, headers=headers)
            assert denied.status_code == 409 and denied.json()["code"] == "SERVICE_SELECTION_REQUIRED"
            assert not db.scalar(select(QueryTask).where(QueryTask.idempotency_key == headers["Idempotency-Key"]))
            # A deliberate existing local API caller retains its explicit contract.
            body["service_mode"] = "local"
            accepted = client.post("/knowledge/queries", json=body, headers=headers)
            assert accepted.status_code == 202
            replay = client.post("/knowledge/queries", json=body, headers=headers)
            assert replay.status_code == 202 and replay.json()["task_id"] == accepted.json()["task_id"]
            task = db.get(QueryTask, accepted.json()["task_id"], populate_existing=True)
            assert task.input_snapshot.get("service_mode", "local") == "local"
    finally:
        app.state.engine.dispose()
