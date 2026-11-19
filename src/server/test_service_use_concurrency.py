"""The final physical-call slot is conserved by independent PostgreSQL sessions."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from .test_postgres_recovery import pg_database
from .models import User
from . import service_admission as admission, service_use as use
from .service_models import ServiceProfile
from .service_use_models import ServiceSelection, ServiceUseCall, ServiceUsePin
from .synthetic_provider import run_synthetic_provider
from .testing_service_use import attest_synthetic_input
from .services import seed_reference_data
from .provider_transport import ProviderError

pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="explicit isolated PostgreSQL required")


def test_last_call_budget_is_consumed_once_across_postgres_sessions(pg_database, monkeypatch):
    engine, _ = pg_database
    env = "DATACENTER_SYNTHETIC_BUDGET_RACE"
    token = "SYNTHETIC-budget-race-secret"
    monkeypatch.setenv(env, token)
    with run_synthetic_provider(credential=token) as provider:
        endpoints = {"synthetic": admission.LocalEndpoint(provider.port, env)}
        with Session(engine, expire_on_commit=False) as db:
            seed_reference_data(db)
            actor = User(username="SYNTHETIC-budget-race", display_name="Synthetic budget", roles=["system_admin"], status="active")
            db.add(actor); db.flush()
            profile = ServiceProfile(kind="generation", label="SYNTHETIC race", created_by=actor.id)
            db.add(profile); db.flush()
            revision = admission.create_revision(db, profile, "synthetic", "openai-chat-v1", "synthetic-chat", endpoints)
            test = admission.new_test(db, revision, actor.id, endpoints); db.commit()
            assert admission.run_test(db, test.id, endpoints).state == "succeeded"
            preview = admission.preview_change(db, profile, actor.id, "activate", revision.id, endpoints)
            admission.apply_change(db, profile, actor.id, "activate", preview.id, endpoints)
            selection = ServiceSelection(purpose="query.answer"); db.add(selection); db.flush()
            preview = use.preview_selection(db, actor.id, selection.purpose, "select", revision.id, 0, endpoints)
            use.apply_selection(db, selection, actor.id, "select", preview.id, endpoints)
            pin = use.pin_use(db, actor.id, selection.purpose, "task:SYNTHETIC-race", endpoints)
            attest_synthetic_input(db, actor.id, pin.purpose, pin.resource_key, b"SYNTHETIC proof")
            db.commit(); pin_id = pin.id
            for n in range(11):
                call = use.reserve_call(db, pin, endpoints, f"setup:{n}", "POST", "/v1/chat/completions", b"{}", b"SYNTHETIC proof")
                use.settle_call(db, call, "failed", error="SYNTHETIC_NO_WIRE")
                db.commit()
        barrier = threading.Barrier(2)
        def contender(key):
            with Session(engine) as db:
                pin = db.get(ServiceUsePin, pin_id)
                barrier.wait(timeout=10)
                try:
                    call = use.reserve_call(db, pin, endpoints, key, "POST", "/v1/chat/completions", b"{}", b"SYNTHETIC proof")
                    use.settle_call(db, call, "failed", error="SYNTHETIC_NO_WIRE")
                    db.commit()
                    return "reserved"
                except ProviderError as exc:
                    db.rollback()
                    return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(contender, ["race:1", "race:2"]))
        assert sorted(results) == ["SERVICE_CALL_BUDGET", "reserved"]
        with Session(engine) as db:
            assert db.get(ServiceUsePin, pin_id).calls_used == 12
            assert len(db.scalars(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin_id)).all()) == 12
        assert len(provider.requests) == 1  # Fixed self-test only; reservations are explicitly not physical sends.
