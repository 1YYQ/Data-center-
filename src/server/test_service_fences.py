"""Regression checks for local service admission and database attempt fences."""
from datetime import timedelta
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from . import service_admission as gate
from .inference_adapters import ChatAdapter
from .models import User, utc_now
from .provider_transport import ProviderError
from .service_models import ServiceProfile, ServiceRequestCall, ServiceRevision, ServiceTestRun
from .services import SafeError
from .test_postgres_recovery import pg_database
from .test_services import fixture_db


CREDENTIAL_ENV = "DATACENTER_SYNTHETIC_FENCE_TOKEN"
TOKEN = "synthetic-fence-before"
ROTATED_TOKEN = "synthetic-fence-after"
CHECKPOINT = {"batch_id": "synthetic-fence-batch", "data_id": "synthetic-fence-data"}
requires_postgres = pytest.mark.skipif(
    not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="requires an explicit local PostgreSQL test URL",
)


@pytest.fixture
def endpoints(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, TOKEN)
    return {"synthetic-fence": gate.LocalEndpoint(31459, CREDENTIAL_ENV, allowed_models=("synthetic-chat", "vlm"))}


def make_profile(db, kind):
    actor = User(username="service-fence-" + uuid4().hex, display_name="SYNTHETIC service admin",
                 status="active", roles=["system_admin"])
    db.add(actor)
    db.flush()
    profile = ServiceProfile(kind=kind, label="SYNTHETIC fences", created_by=actor.id)
    db.add(profile)
    db.flush()
    return actor, profile


def make_run(db, endpoints, *, kind="generation", state="pending"):
    actor, profile = make_profile(db, kind)
    model = "synthetic-chat" if kind == "generation" else "vlm"
    revision = gate.create_revision(db, profile, "synthetic-fence", gate.ADAPTERS[kind], model, endpoints)
    run = gate.new_test(db, revision, actor.id, endpoints)
    if state == "running":
        run.state, run.attempt = "running", 4
        run.lease_until = utc_now() - timedelta(minutes=1)
        run.checkpoint = CHECKPOINT.copy()
    db.commit()
    return profile, run


def test_generation_model_must_come_from_the_deployment_allowlist(fixture_db, endpoints):
    db, _ = fixture_db
    _, profile = make_profile(db, "generation")
    with pytest.raises(ProviderError, match="^SERVICE_MODEL_NOT_ADMITTED$"):
        gate.create_revision(db, profile, "synthetic-fence", gate.ADAPTERS["generation"],
                             "synthetic/arbitrary-user-supplied-id", endpoints)
    assert profile.revision == 0
    assert db.scalars(select(ServiceRevision).where(ServiceRevision.profile_id == profile.id)).all() == []


def test_secret_rotation_during_intent_commit_never_substitutes_new_wire_credentials(fixture_db, endpoints, monkeypatch):
    db, _ = fixture_db
    profile, run = make_run(db, endpoints)
    run.state, run.attempt, run.lease_until = "running", 1, utc_now() + timedelta(minutes=2)
    db.commit()
    observed = []

    class LocalResponse:
        status = 200
        length = 0

        def read(self, _maximum):
            return b"SYNTHETIC response"

    class RecordingConnection:
        def __init__(self, hostname, port, *, timeout):
            assert (hostname, port, timeout) == ("127.0.0.1", 31459, 2)

        def request(self, method, target, *, body, headers):
            with Session(db.bind) as observer:
                call = observer.scalar(select(ServiceRequestCall).where(ServiceRequestCall.test_run_id == run.id))
                assert call.state == "dispatching"
            observed.append((method, target, headers["Authorization"]))

        def getresponse(self):
            return LocalResponse()

        def close(self):
            pass

    def rotate(_session):
        monkeypatch.setenv(CREDENTIAL_ENV, ROTATED_TOKEN)

    monkeypatch.setattr(gate.http.client, "HTTPConnection", RecordingConnection)
    event.listen(db, "after_commit", rotate, once=True)
    with pytest.raises(ProviderError, match="^SERVICE_ADMISSION_STALE$"):
        gate.AdmissionTransport(db, run, endpoints, profile.revision).request("POST", "/v1/chat/completions")
    assert observed == [("POST", "/v1/chat/completions", "Bearer " + TOKEN)]
    call = db.scalar(select(ServiceRequestCall).where(ServiceRequestCall.test_run_id == run.id))
    assert call.state == "received"


def assert_stale_resume_is_fenced(db, endpoints):
    _, stale = make_run(db, endpoints, kind="parsing", state="running")
    attempt = stale.attempt
    with Session(db.bind) as worker:
        current = worker.get(ServiceTestRun, stale.id)
        current.attempt = attempt + 1
        current.lease_until = utc_now() + timedelta(minutes=2)
        worker.commit()
    with pytest.raises(SafeError) as exc:
        gate.prepare_resume(db, stale)
    assert exc.value.code == "SERVICE_TEST_IN_PROGRESS"
    db.rollback()
    db.refresh(stale)
    assert stale.state == "running" and stale.attempt == attempt + 1
    assert stale.checkpoint == CHECKPOINT


def test_stale_resume_cannot_reclaim_a_newer_running_attempt(fixture_db, endpoints):
    db, _ = fixture_db
    assert_stale_resume_is_fenced(db, endpoints)


def assert_stale_intent_is_fenced(db, endpoints, monkeypatch):
    profile, run = make_run(db, endpoints)
    run.state, run.attempt, run.lease_until = "running", 1, utc_now() + timedelta(minutes=2)
    db.commit()

    def supersede_after_run_read():
        with Session(db.bind) as worker:
            current = worker.get(ServiceTestRun, run.id)
            current.attempt = 2
            worker.commit()

    def no_wire(_hostname, _port, *, timeout):
        pytest.fail("stale attempt tried to create a connection")

    monkeypatch.setattr(gate.http.client, "HTTPConnection", no_wire)
    transport = gate.AdmissionTransport(db, run, endpoints, profile.revision,
                                        authority_check=supersede_after_run_read)
    with pytest.raises(ProviderError, match="^SERVICE_ADMISSION_STALE$"):
        transport.request("POST", "/v1/chat/completions")
    db.rollback()
    assert db.get(ServiceTestRun, run.id).attempt == 2
    assert db.scalars(select(ServiceRequestCall).where(ServiceRequestCall.test_run_id == run.id)).all() == []


def test_stale_attempt_cannot_record_or_send_a_call(fixture_db, endpoints, monkeypatch):
    db, _ = fixture_db
    assert_stale_intent_is_fenced(db, endpoints, monkeypatch)


@requires_postgres
def test_postgres_rechecks_resume_and_request_cas_on_the_current_row(pg_database, endpoints, monkeypatch):
    engine, _ = pg_database
    with Session(engine, autoflush=False, expire_on_commit=False) as db:
        assert_stale_resume_is_fenced(db, endpoints)
        assert_stale_intent_is_fenced(db, endpoints, monkeypatch)


def test_worker_losing_its_attempt_after_the_final_check_cannot_commit_success(fixture_db, endpoints, monkeypatch):
    db, _ = fixture_db
    _, run = make_run(db, endpoints)
    def result_after_claim(_adapter, _messages):
        return {"model": "synthetic-chat", "text": "SYNTHETIC OK", "usage": None}

    def lose_attempt_after_check(_transport):
        # Exercise the terminal compare-and-swap separately from the live fence.
        with Session(db.bind) as worker:
            newer = worker.get(ServiceTestRun, run.id)
            newer.attempt += 1
            newer.state = "running"
            newer.lease_until = utc_now() + timedelta(minutes=2)
            worker.commit()

    monkeypatch.setattr(ChatAdapter, "complete", result_after_claim)
    monkeypatch.setattr(gate.AdmissionTransport, "check", lose_attempt_after_check)
    refreshed = gate.run_test(db, run.id, endpoints)
    assert refreshed.attempt == 2 and refreshed.state == "running"
    assert not refreshed.result and refreshed.completed_at is None and refreshed.valid_until is None
