"""Purpose-selected local business services. Exact synthetic inputs only.

Selection is policy, a pin is identity, and a private input attestation bounds
bytes. All three plus current business authorization are required before send.
"""
from __future__ import annotations
from datetime import timedelta
import http.client
import json
import re
from urllib.parse import urlsplit
from sqlalchemy import select, update
from . import local_bge
from .models import User, utc_now
from .service_models import ServiceProfile, ServiceRevision, ServiceTestRun
from .service_use_models import (ServiceSelection, ServiceSelectionPreview, ServiceUsePin,
    SyntheticServiceInput, ServiceUseCall, ServiceTaskSelection)
from . import service_admission as admission
from .provider_transport import ProviderError
from .services import SafeError

PURPOSES = {"document.parse": "parsing", "document.embed": "embedding",
            "query.answer": "generation", "query.summary": "generation", "query.compaction": "generation"}
LOCAL_EMBEDDING_REQUEST = "LOCAL:embedding"


def _purpose(purpose):
    if purpose not in PURPOSES:
        raise SafeError("SERVICE_PURPOSE_INVALID", "不支持的服务用途", 422)


def selected_revision(db, purpose, revision_id, endpoints):
    _purpose(purpose)
    revision = db.get(ServiceRevision, revision_id)
    profile = db.scalar(select(ServiceProfile).where(ServiceProfile.id == revision.profile_id)
        .with_for_update(read=True).execution_options(populate_existing=True)) if revision else None
    if not profile or profile.kind != PURPOSES[purpose] or not profile.enabled or profile.active_revision_id != revision.id:
        raise ProviderError("SERVICE_SELECTION_UNAVAILABLE")
    if isinstance(endpoints.get(revision.endpoint_ref), admission.BailianEndpoint) and purpose != "query.answer":
        raise ProviderError("SERVICE_PURPOSE_NOT_PERMITTED")
    bound = admission.binding(revision, endpoints)
    test = db.scalar(select(ServiceTestRun).where(ServiceTestRun.revision_id == revision.id,
        ServiceTestRun.binding_hash == bound, ServiceTestRun.state == "succeeded")
        .order_by(ServiceTestRun.completed_at.desc()).execution_options(populate_existing=True))
    if not test or not admission.future(test.valid_until):
        raise ProviderError("SERVICE_TEST_REQUIRED")
    return revision, profile, test, bound


def preview_selection(db, actor_id, purpose, action, revision_id, selection_revision, endpoints):
    _purpose(purpose)
    revision = profile = test = bound = None
    if action == "select":
        revision, profile, test, bound = selected_revision(db, purpose, revision_id, endpoints)
    row = ServiceSelectionPreview(actor_id=actor_id, purpose=purpose, action=action,
        selection_revision=selection_revision, revision_id=revision.id if revision else None,
        test_run_id=test.id if test else None, binding_hash=bound,
        profile_revision=profile.revision if profile else None, expires_at=utc_now() + timedelta(minutes=5))
    db.add(row)
    db.flush()
    return row


def apply_selection(db, row, actor_id, action, preview_id, endpoints):
    preview = db.get(ServiceSelectionPreview, preview_id, populate_existing=True)
    if (not preview or preview.purpose != row.purpose or preview.actor_id != actor_id
            or preview.action != action or preview.consumed or not admission.future(preview.expires_at)
            or preview.selection_revision != row.revision):
        raise ProviderError("SERVICE_PREVIEW_STALE")
    if action == "select":
        revision, profile, test, bound = selected_revision(db, row.purpose, preview.revision_id, endpoints)
        if (profile.revision != preview.profile_revision or bound != preview.binding_hash
                or test.id != preview.test_run_id):
            raise ProviderError("SERVICE_PREVIEW_STALE")
        row.revision_id, row.test_run_id, row.binding_hash = revision.id, test.id, bound
        row.enabled = True
    else:
        row.enabled = False
        row.epoch += 1  # Re-enable never revives an old accepted pin.
    row.revision += 1
    preview.consumed = True
    db.flush()
    return row


def pin_use(db, actor_id, purpose, resource_key, endpoints):
    _purpose(purpose)
    # Snapshot, then lock profile before selection. Recheck prevents a mixed pin.
    row = db.get(ServiceSelection, purpose, populate_existing=True)
    if not row or not row.enabled:
        raise ProviderError("SERVICE_SELECTION_REQUIRED")
    revision_id = row.revision_id
    revision, profile, test, bound = selected_revision(db, purpose, revision_id, endpoints)
    row = db.scalar(select(ServiceSelection).where(ServiceSelection.purpose == purpose)
        .with_for_update(read=True).execution_options(populate_existing=True))
    if not row.enabled or row.revision_id != revision_id or row.binding_hash != bound or row.test_run_id != test.id:
        raise ProviderError("SERVICE_SELECTION_CHANGED")
    pin = ServiceUsePin(actor_id=actor_id, purpose=purpose, resource_key=resource_key,
        revision_id=revision.id, test_run_id=test.id, binding_hash=bound,
        selection_epoch=row.epoch, profile_epoch=profile.use_epoch,
        expires_at=test.valid_until, max_calls=12, calls_used=0)
    db.add(pin)
    db.flush()
    return pin


def check_pin(db, pin, endpoints, *, lock=True):
    db.refresh(pin)
    revision = db.get(ServiceRevision, pin.revision_id, populate_existing=True)
    profile_query = select(ServiceProfile).where(ServiceProfile.id == revision.profile_id) if revision else None
    selection_query = select(ServiceSelection).where(ServiceSelection.purpose == pin.purpose)
    if lock:
        profile_query = profile_query.with_for_update(read=True) if profile_query is not None else None
        selection_query = selection_query.with_for_update(read=True)
    profile = db.scalar(profile_query.execution_options(populate_existing=True)) if profile_query is not None else None
    row = db.scalar(selection_query.execution_options(populate_existing=True))
    test = db.get(ServiceTestRun, pin.test_run_id, populate_existing=True)
    if (not profile or not row or not profile.enabled or not row.enabled or profile.kind != PURPOSES.get(pin.purpose)
            or profile.use_epoch != pin.profile_epoch or row.epoch != pin.selection_epoch
            or not admission.future(pin.expires_at) or not test or test.state != "succeeded"
            or test.revision_id != pin.revision_id or not admission.future(test.valid_until)
            or test.binding_hash != pin.binding_hash):
        raise ProviderError("SERVICE_USE_REVOKED")
    endpoint = endpoints.get(revision.endpoint_ref)
    if isinstance(endpoint, local_bge.LocalBgeEndpoint) and pin.purpose == "document.embed":
        credential = trust_bytes = tls_context = None
    elif isinstance(endpoint, admission.LocalEndpoint):
        credential = endpoint.validate()
        trust_bytes, tls_context = endpoint.trust()
    else:
        raise ProviderError("SERVICE_ENDPOINT_NOT_ADMITTED")
    if admission.binding(revision, endpoints, credential, trust_bytes) != pin.binding_hash:
        raise ProviderError("SERVICE_ADMISSION_STALE")
    return revision, endpoint, credential, tls_context


def prove_input(db, pin, payload):
    if type(payload) is not bytes:
        raise ProviderError("SERVICE_SYNTHETIC_INPUT_REQUIRED")
    proof = db.scalar(select(SyntheticServiceInput).where(SyntheticServiceInput.actor_id == pin.actor_id,
        SyntheticServiceInput.purpose == pin.purpose, SyntheticServiceInput.resource_key == pin.resource_key,
        SyntheticServiceInput.input_hash == admission.digest(payload), SyntheticServiceInput.revoked.is_(False),
        SyntheticServiceInput.expires_at > utc_now()).with_for_update(read=True).execution_options(populate_existing=True))
    if not proof or proof.provenance != "private-local-synthetic-fixture-v1":
        raise ProviderError("SERVICE_SYNTHETIC_INPUT_REQUIRED")
    return proof


def get_task_pin(db, task_id, operation):
    marker = db.get(ServiceTaskSelection, task_id, populate_existing=True)
    if not marker:
        return None
    purpose = {"answer": "query.answer", "summary": "query.summary", "compaction": "query.compaction"}.get(operation)
    pin_id = marker.pin_ids.get(purpose) if purpose else None
    pin = db.get(ServiceUsePin, pin_id) if pin_id else None
    if (not marker.required or not pin or pin.purpose != purpose or pin.resource_key != "task:" + task_id):
        raise ProviderError("SERVICE_PURPOSE_NOT_PERMITTED")
    return pin


def reserve_call(db, pin, endpoints, request_key, method, target, body, proof_payload,
                 authority_check=None, purpose="api"):
    check_pin(db, pin, endpoints)
    if authority_check:
        authority_check()
    actor = db.scalar(select(User).where(User.id == pin.actor_id).with_for_update(read=True)
        .execution_options(populate_existing=True))
    if not actor or actor.status != "active":
        raise ProviderError("SERVICE_ACTOR_REVOKED")
    prove_input(db, pin, proof_payload)
    prior = db.scalar(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin.id, ServiceUseCall.request_key == request_key))
    if prior:
        if (prior.method, prior.purpose, prior.target_hash, prior.payload_hash, prior.input_hash) != (
                method, purpose, admission.digest(target.encode()), admission.digest(body), admission.digest(proof_payload)):
            raise ProviderError("SERVICE_REQUEST_MISMATCH")
        return prior
    if not request_key or len(request_key) > 200 or type(body) is not bytes or len(body) > 65536:
        raise ProviderError("SERVICE_REQUEST_BUDGET")
    expected = pin.calls_used
    changed = db.execute(update(ServiceUsePin).where(ServiceUsePin.id == pin.id,
        ServiceUsePin.calls_used == expected, ServiceUsePin.calls_used < ServiceUsePin.max_calls)
        .values(calls_used=ServiceUsePin.calls_used + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise ProviderError("SERVICE_CALL_BUDGET")
    db.refresh(pin)
    call = ServiceUseCall(pin_id=pin.id, request_key=request_key, ordinal=pin.calls_used,
        method=method, purpose=purpose, target_hash=admission.digest(target.encode()), payload_hash=admission.digest(body),
        input_hash=admission.digest(proof_payload), state="dispatching")
    db.add(call)
    db.flush()
    return call


def settle_call(db, call, state, status=None, response_hash=None, error=None, usage=None):
    if state not in {"received", "failed", "unknown"}:
        raise ValueError("A physical call requires an observed outcome or explicit unknown")
    current = db.scalar(select(ServiceUseCall).where(ServiceUseCall.id == call.id).with_for_update()
        .execution_options(populate_existing=True))
    if not current or current.state != "dispatching":
        return  # A stale worker cannot overwrite a durable reconciliation.
    call.state, call.response_status, call.response_hash, call.safe_error = state, status, response_hash, error
    call.usage = usage  # Absent usage and cost remain unknown, never fabricated zero.
    db.flush()


class BusinessTransport:
    def __init__(self, db, pin, endpoints, authority_check, source_bytes, source_guard=None, download_sink=None):
        self.db, self.pin, self.endpoints = db, pin, endpoints
        self.authority_check, self.source_bytes = authority_check, source_bytes
        self.source_guard = source_guard
        self.download_sink = download_sink

    def check(self):
        if self.source_guard:
            self.source_guard()
        values = check_pin(self.db, self.pin, self.endpoints)
        self.authority_check()
        prove_input(self.db, self.pin, self.source_bytes())
        return values

    def embed_local(self):
        revision, endpoint, *_ = self.check()
        if not isinstance(endpoint, local_bge.LocalBgeEndpoint) or revision.adapter != local_bge.ADAPTER:
            raise ProviderError("SERVICE_DESTINATION_DENIED")
        payload = self.source_bytes()
        prior = self.db.scalar(select(ServiceUseCall.id).where(ServiceUseCall.pin_id == self.pin.id,
            ServiceUseCall.request_key == LOCAL_EMBEDDING_REQUEST))
        if prior:
            raise ProviderError("SERVICE_RECONCILIATION_REQUIRED")
        call = reserve_call(self.db, self.pin, self.endpoints, LOCAL_EMBEDDING_REQUEST, "LOCAL", revision.model,
                            payload, payload, self.authority_check, purpose="embedding")
        self.db.commit()
        try:
            _, endpoint, *_ = self.check()
            if self.source_bytes() != payload:
                raise ProviderError("SERVICE_REQUEST_MISMATCH")
            self.db.commit()
            result = endpoint.embed(json.loads(payload))
        except (ProviderError, SafeError) as error:
            self.db.rollback()
            settle_call(self.db, call, "failed", error=error.code)
            self.db.commit()
            raise
        settle_call(self.db, call, "received", response_hash=admission.digest(result["vectors"]))
        self.db.commit()
        self.check()
        if call.state != "received":
            raise ProviderError("SERVICE_RECONCILIATION_REQUIRED")
        return result

    def request(self, method, target, *, body=b"", purpose="api", content_type="application/json"):
        revision, endpoint, credential, tls_context = self.check()
        if not isinstance(endpoint, admission.LocalEndpoint):
            raise ProviderError("SERVICE_DESTINATION_DENIED")
        try:
            parts = urlsplit(target)
            port = parts.port
        except ValueError:
            raise ProviderError("SERVICE_DESTINATION_DENIED") from None
        if parts.scheme or parts.netloc:
            if (parts.scheme != endpoint.scheme or parts.hostname != "127.0.0.1" or port != endpoint.port
                    or parts.username is not None or parts.password is not None or parts.fragment):
                raise ProviderError("SERVICE_DESTINATION_DENIED")
            path = parts.path + ("?" + parts.query if parts.query else "")
        else:
            path = target
        allowed = ((purpose == "api" and method == "POST" and path == {"parsing": "/api/v4/file-urls/batch",
            "generation": "/v1/chat/completions", "embedding": "/v1/embeddings"}[PURPOSES[self.pin.purpose]])
            or (self.pin.purpose == "document.parse" and purpose == "api" and method == "GET"
                and re.fullmatch(r"/api/v4/extract-results/batch/[A-Za-z0-9_.-]{1,128}", path))
            or (self.pin.purpose == "document.parse" and parts.scheme == endpoint.scheme
                and (purpose, method) in {("upload", "PUT"), ("download", "GET")}))
        if not allowed or not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n#\\"):
            raise ProviderError("SERVICE_DESTINATION_DENIED")
        if method == "PUT" and body != self.source_bytes():
            raise ProviderError("SERVICE_SYNTHETIC_INPUT_REQUIRED")
        if method == "POST":
            try:
                data = json.loads(body)
                if self.pin.purpose == "document.embed":
                    if (set(data) != {"model", "encoding_format", "input"} or data["model"] != revision.model
                            or data["encoding_format"] != "float" or data["input"] != json.loads(self.source_bytes())):
                        raise ValueError()
                elif self.pin.purpose == "document.parse":
                    if (set(data) != {"files", "model_version", "enable_formula", "enable_table"}
                            or data["model_version"] != "vlm" or data["enable_formula"] is not True
                            or data["enable_table"] is not True or len(data["files"]) != 1
                            or set(data["files"][0]) != {"name", "data_id"}
                            or data["files"][0]["name"] != "synthetic.pdf"
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", data["files"][0]["data_id"])):
                        raise ValueError()
                else:
                    # Pi has its own pinned SDK body validation and call ledger.
                    raise ValueError()
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ProviderError("SERVICE_REQUEST_MISMATCH") from None
        # Submission/upload are one physical action per pin. A read can poll
        # again within the same durable total budget, never with new submission.
        key = method + ":" + purpose if method in {"POST", "PUT"} else f"read:{self.pin.calls_used + 1}"
        prior = self.db.scalar(select(ServiceUseCall).where(ServiceUseCall.pin_id == self.pin.id,
            ServiceUseCall.request_key == key))
        if prior:
            raise ProviderError("SERVICE_RECONCILIATION_REQUIRED")
        call = reserve_call(self.db, self.pin, self.endpoints, key, method, target, body,
            self.source_bytes(), self.authority_check, purpose=purpose)
        self.db.commit()
        # Reacquire current source, policy and proof fences after intent commit.
        # They stay held through the bounded socket write, not response waiting.
        try:
            self.check()
        except (ProviderError, SafeError) as error:
            self.db.rollback()
            self.db.refresh(call)
            settle_call(self.db, call, "failed", error=error.code)
            self.db.commit()
            raise
        headers = {"Content-Type": content_type} if content_type else {}
        if purpose == "api":
            headers["Authorization"] = "Bearer " + credential
        connection = (http.client.HTTPSConnection("127.0.0.1", endpoint.port, timeout=2, context=tls_context)
            if endpoint.scheme == "https" else http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=2))
        try:
            connection.request(method, path, body=body, headers=headers)
            self.db.commit()
            response = connection.getresponse()
            raw = response.read(1048577)
            if len(raw) <= 1048576 and response.length:
                raise http.client.IncompleteRead(raw, response.length)
            if len(raw) > 1048576:
                settle_call(self.db, call, "failed", response.status, error="SERVICE_RESPONSE_BUDGET")
            elif not 200 <= response.status < 300:
                settle_call(self.db, call, "failed", response.status, error="SERVICE_UPSTREAM_REJECTED")
            else:
                if purpose == "download" and self.download_sink is not None:
                    try:
                        self.download_sink(raw)
                    except (ProviderError, OSError):
                        settle_call(self.db, call, "received", response.status, admission.digest(raw),
                                    error="PARSE_ARCHIVE_STORAGE_UNAVAILABLE")
                        self.db.commit()
                        raise ProviderError("PARSE_ARCHIVE_STORAGE_UNAVAILABLE") from None
                settle_call(self.db, call, "received", response.status, admission.digest(raw))
            self.db.commit()
        except (OSError, http.client.HTTPException, UnicodeError):
            self.db.rollback()
            self.db.refresh(call)
            settle_call(self.db, call, "unknown", error="SERVICE_OUTCOME_UNKNOWN")
            self.db.commit()
        finally:
            connection.close()
        self.check()
        if call.safe_error:
            raise ProviderError(call.safe_error)
        if call.state != "received":
            raise ProviderError("SERVICE_RECONCILIATION_REQUIRED")
        return raw
