"""Scheduling and shared-resource boundaries, without provider/model calls."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from . import pilot_dispatch as dispatch


def application(events):
    endpoint = object()
    @contextmanager
    def session():
        events.append("session-open")
        try:
            yield object()
        finally:
            events.append("session-close")
    return SimpleNamespace(state=SimpleNamespace(settings=object(), session_factory=session,
        service_endpoints={"existing": endpoint}))


def test_accepted_query_prevents_processing_or_second_endpoint(monkeypatch):
    events = []
    app = application(events)
    monkeypatch.setattr(dispatch, "run_one", lambda *a, **kw: {"state": "succeeded"})
    monkeypatch.setattr(dispatch, "scan_processing", lambda *a, **kw: pytest.fail("query must finish before processing"))
    monkeypatch.setattr(dispatch, "scan_embedding", lambda *a, **kw: pytest.fail("query must finish before embedding"))
    assert dispatch.advance_once(app, gateway_url="http://127.0.0.1:8879", profile_revision=5)["kind"] == "query"
    assert events == []


@pytest.mark.parametrize("processing_count", [0, 1])
def test_idle_query_advances_bounded_work_with_original_endpoint(monkeypatch, processing_count):
    events = []
    app = application(events)
    def query(config, **kwargs):
        assert config is app.state.settings and kwargs["profile_revision"] == 5
        return {"state": "idle"}
    def processing(db, config, endpoints, *, limit):
        assert config is app.state.settings and endpoints is app.state.service_endpoints and limit == 1
        events.append("processing")
        return {"count": processing_count, "jobs": []}
    def embedding(db, config, endpoints, *, limit):
        assert processing_count == 0 and endpoints is app.state.service_endpoints and limit == 1
        events.append("embedding")
        return {"count": 1, "jobs": []}
    monkeypatch.setattr(dispatch, "run_one", query)
    monkeypatch.setattr(dispatch, "scan_processing", processing)
    monkeypatch.setattr(dispatch, "scan_embedding", embedding)
    result = dispatch.advance_once(app, gateway_url="http://127.0.0.1:8879", profile_revision=5)
    assert result["kind"] == ("processing" if processing_count else "embedding")
    assert events.count("session-open") == events.count("session-close")


def test_unknown_processing_failure_does_not_fall_through_or_retry(monkeypatch):
    events = []
    app = application(events)
    monkeypatch.setattr(dispatch, "run_one", lambda *a, **kw: {"state": "idle"})
    def failed(*args, **kwargs):
        raise RuntimeError("uncertain accepted job")
    monkeypatch.setattr(dispatch, "scan_processing", failed)
    monkeypatch.setattr(dispatch, "scan_embedding", lambda *a, **kw: pytest.fail("uncertain work must not fall through"))
    with pytest.raises(RuntimeError, match="uncertain"):
        dispatch.advance_once(app, gateway_url="http://127.0.0.1:8879", profile_revision=5)
    assert events == ["session-open", "session-close"]


def test_duplicate_pilot_port_refuses_before_settings_or_model_loading(monkeypatch):
    import socket
    from . import pilot
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        monkeypatch.setattr(pilot, "load_settings", lambda *a: pytest.fail("occupied port must not load settings or weights"))
        with pytest.raises(OSError):
            pilot.serve(port=listener.getsockname()[1])
