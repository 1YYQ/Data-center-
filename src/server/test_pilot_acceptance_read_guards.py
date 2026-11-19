"""In-flight progress is cheap and content-free; final reads retain full gates."""
import importlib

from sqlalchemy import select

from .models import AuthorizationGrant, utc_now
from .test_answer_share import share_http
from .test_execution import _create, execution_http
from .test_runtime_gate import local_upstream, runtime_db


def test_owner_progress_does_not_replay_evidence_and_other_readers_still_require_access(share_http, monkeypatch):
    f = share_http
    task = _create(f, query="SYNTHETIC secret input must not appear in progress")
    app_module = importlib.import_module(f"{__package__}.app")

    def forbid_full_replay(*_):
        raise AssertionError("Safe owner progress must not reconstruct evidence")

    monkeypatch.setattr(app_module, "can_read_task", forbid_full_replay)
    progress = f.client.get(f"/tasks/{task.id}/status")
    assert progress.status_code == 200
    assert set(progress.json()) == {"id", "status", "revision"}
    assert progress.json()["status"] == "pending" and "secret" not in progress.text
    monkeypatch.setattr(app_module, "can_read_task", lambda *_: False)
    assert f.no_dr_client.get(f"/tasks/{task.id}/status").status_code == 404
    assert f.client.get(f"/tasks/{task.id}").json()["status"] == "access_limited"
    task.status = "succeeded"; f.db.commit()
    assert f.client.get(f"/tasks/{task.id}/status").json()["status"] == "access_limited"
    task.status = "pending"
    for grant in f.db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor.id,
                                                            AuthorizationGrant.operation == "knowledge.query")):
        grant.revoked_at = utc_now()
    f.db.commit()
    assert f.client.get(f"/tasks/{task.id}/status").json()["status"] == "access_limited"
