"""Strict synthetic service admission. No enterprise dispatch is implemented here.

Endpoint registration belongs to the trusted local process, never HTTP input.
Every byte transmitted by this module comes from the server-owned fixture.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import local_bge
from .models import User, utc_now
from .provider_transport import ProviderError
from .service_models import ServiceProfile, ServiceRevision, ServiceTestRun, ServiceChangePreview, ServiceRequestCall
from .services import SafeError, has_permission

FIXTURE = "service-synthetic-v1"
ADAPTERS = {"generation": "openai-chat-v1", "embedding": "bge-m3-v1", "parsing": "mineru-v4-legacy-v1"}
PI_ADAPTER = "pi-ai-1.5.2/openai-completions"
PI_API_FAMILY = "openai-chat-completions-sse-v1"
PI_TOKENIZER = "utf8-byte-ceiling-v1"
PI_CAPABILITIES = ("text", "function_tools", "sse", "stream_usage")
LOOPBACK_EGRESS = "synthetic-loopback-only-v1"
GENERATION_EGRESS = "generation-https-pinned-v1"
CHAT_COMPLETIONS = "/v1/chat/completions"
BAILIAN_HOST = "dashscope.aliyuncs.com"
BAILIAN_MODEL = "qwen3.8-flash"
BAILIAN_ENDPOINT_REF = "bailian-beijing-qwen38-flash"
BAILIAN_CHAT_PATH = "/compatible-mode/v1/chat/completions"
BAILIAN_WIRE = "bailian-beijing-qwen38-flash-v1"
BAILIAN_ENV = "DATACENTER_BAILIAN_SERVICE_ENDPOINT"
BAILIAN_OPTIONS = {"enable_thinking": False, "preserve_thinking": False}


def generation_model(model, *, api_family=PI_API_FAMILY, tokenizer=PI_TOKENIZER,
                     context_window=8192, max_output_tokens=128, capabilities=PI_CAPABILITIES):
    """A bounded protocol shape, independent of enterprise provider admission.

    The byte ceiling is an explicit conservative counting policy for this
    profile. A provider requiring its exact tokenizer needs a different profile.
    """
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", model)
            or api_family != PI_API_FAMILY or tokenizer != PI_TOKENIZER
            or type(context_window) is not int or not 4096 <= context_window <= 65536
            or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= min(4096, context_window // 4)
            or not isinstance(capabilities, (list, tuple)) or tuple(capabilities) != PI_CAPABILITIES):
        raise ProviderError("RUNTIME_SERVICE_PROTOCOL_INCOMPATIBLE")
    return {"model": model, "adapter": PI_ADAPTER, "service_adapter": ADAPTERS["generation"],
            "api_family": api_family, "tokenizer": tokenizer, "context_window": context_window,
            "max_output_tokens": max_output_tokens, "capabilities": list(capabilities)}


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def future(value):
    return bool(value and value.replace(tzinfo=value.tzinfo or timezone.utc) > utc_now())


@dataclass(frozen=True)
class LocalEndpoint:
    """Trusted deployment entry; an environment ref, never a secret value."""
    port: int
    credential_env: str
    revision: str = "1"
    credential_revision: str = "1"
    allowed_models: tuple[str, ...] = ("synthetic-chat", "BAAI/bge-m3", "Pro/BAAI/bge-m3", "vlm")
    scheme: str = "http"
    tls_ca_file: str | None = None
    generation_models: dict[str, dict] | None = None
    hostname: str = "127.0.0.1"
    peer_address: str | None = None
    egress: str = LOOPBACK_EGRESS

    @property
    def chat_path(self):
        return CHAT_COMPLETIONS

    def credential_ref_allowed(self, remote):
        env_prefix = "DATACENTER_GENERATION_" if remote else "DATACENTER_SYNTHETIC_"
        return re.fullmatch(env_prefix + r"[A-Z0-9_]{1,60}", self.credential_env)

    def generation_profiles(self):
        if self.generation_models is None:
            return {}
        if not isinstance(self.generation_models, dict) or not set(self.generation_models) <= set(self.allowed_models):
            raise ProviderError("RUNTIME_SERVICE_PROTOCOL_INCOMPATIBLE")
        fields = {"api_family", "tokenizer", "context_window", "max_output_tokens", "capabilities"}
        if any(not isinstance(options, dict) or set(options) != fields for options in self.generation_models.values()):
            raise ProviderError("RUNTIME_SERVICE_PROTOCOL_INCOMPATIBLE")
        try:
            return {model: generation_model(model, **options) for model, options in self.generation_models.items()}
        except TypeError:
            raise ProviderError("RUNTIME_SERVICE_PROTOCOL_INCOMPATIBLE") from None

    def validate(self):
        remote = self.egress == GENERATION_EGRESS
        minimum_port = 1 if remote else 1024
        if (type(self.port) is not int or not minimum_port <= self.port <= 65535
                or not self.credential_ref_allowed(remote)
                or not self.revision or not self.credential_revision or self.scheme not in {"http", "https"}
                or self.scheme == "http" and self.tls_ca_file is not None
                or not isinstance(self.allowed_models, (list, tuple)) or not self.allowed_models
                or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}", x) for x in self.allowed_models)):
            raise ProviderError("SERVICE_ENDPOINT_NOT_ADMITTED")
        if remote:
            if (self.scheme != "https" or not self.tls_ca_file or not self.generation_profiles()
                    or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", self.hostname)):
                raise ProviderError("SERVICE_DESTINATION_DENIED")
            try:
                peer = ipaddress.ip_address(self.peer_address)
            except ValueError:
                raise ProviderError("SERVICE_DESTINATION_DENIED") from None
            if not peer.is_global or peer.is_multicast or peer.is_reserved:
                raise ProviderError("SERVICE_DESTINATION_DENIED")
        elif self.egress != LOOPBACK_EGRESS or self.hostname != "127.0.0.1" or self.peer_address is not None:
            raise ProviderError("SERVICE_DESTINATION_DENIED")
        secret = os.environ.get(self.credential_env, "")
        if not secret or len(secret) > 4096 or any(c in secret for c in "\r\n"):
            raise ProviderError("SERVICE_CREDENTIAL_NOT_CONFIGURED")
        return secret

    def trust(self):
        if self.scheme == "http":
            return b"", None
        try:
            if not self.tls_ca_file:
                raise ValueError()
            with Path(self.tls_ca_file).open("rb") as file:
                raw = file.read(1048577)
            if not raw or len(raw) > 1048576:
                raise ValueError()
            context = ssl.create_default_context(cadata=raw.decode("ascii"))
            # Defaults are deliberately retained: CERT_REQUIRED + hostname check.
            return raw, context
        except (OSError, ValueError, UnicodeError, ssl.SSLError):
            raise ProviderError("SERVICE_TLS_NOT_CONFIGURED") from None

    def binding(self, secret=None, trust_bytes=None):
        secret = self.validate() if secret is None else secret
        if trust_bytes is None:
            trust_bytes, _ = self.trust()
        return digest({"origin": [self.scheme, self.hostname, self.port], "network": self.egress,
                       "peer_address": self.peer_address,
                       "revision": self.revision, "credential_revision": self.credential_revision,
                       "credential_ref": self.credential_env, "chat_path": self.chat_path,
                       "credential_fingerprint": digest(secret.encode()), "fixture": FIXTURE,
                       "allowed_models": self.allowed_models, "tls_trust_fingerprint": digest(trust_bytes),
                       "generation_profiles": self.generation_profiles()})

    def connect(self, timeout, tls_context):
        if self.scheme == "http":
            return http.client.HTTPConnection(self.hostname, self.port, timeout=timeout)
        connection = http.client.HTTPSConnection(self.hostname, self.port, timeout=timeout, context=tls_context)
        if self.egress == GENERATION_EGRESS:
            # TLS/SNI use the approved hostname. The socket uses only its pinned
            # address, independent of proxy environment and mutable DNS.
            def dial(address, timeout, source_address=None):
                return socket.create_connection((self.peer_address, address[1]), timeout, source_address)
            connection._create_connection = dial
        return connection


class BailianEndpoint(LocalEndpoint):
    @property
    def chat_path(self):
        return BAILIAN_CHAT_PATH

    def credential_ref_allowed(self, remote):
        return remote and self.credential_env == "DASHSCOPE_API_KEY"

    def generation_profiles(self):
        return {BAILIAN_MODEL: {**generation_model(BAILIAN_MODEL, context_window=32768, max_output_tokens=2048),
                                "wire_protocol": BAILIAN_WIRE}}

    def validate(self):
        if ((self.scheme, self.hostname, self.port, self.allowed_models) != ("https", BAILIAN_HOST, 443, (BAILIAN_MODEL,))
                or self.generation_models is not None):
            raise ProviderError("SERVICE_DESTINATION_DENIED")
        return super().validate()


def bailian_endpoint(*, peer_address, tls_ca_file, revision="1", credential_revision="1"):
    """Deployment-only pin for the approved Beijing compatibility route."""
    return BailianEndpoint(443, "DASHSCOPE_API_KEY", revision=revision, credential_revision=credential_revision,
                           allowed_models=(BAILIAN_MODEL,), scheme="https", tls_ca_file=tls_ca_file,
                           hostname=BAILIAN_HOST, peer_address=peer_address, egress=GENERATION_EGRESS)


def generation_profile(revision, endpoint, *, explicit=False):
    if revision.adapter != ADAPTERS["generation"] or revision.model not in endpoint.allowed_models:
        raise ProviderError("RUNTIME_SERVICE_PROTOCOL_INCOMPATIBLE")
    approved = endpoint.generation_profiles().get(revision.model)
    if approved is None and explicit:
        raise ProviderError("RUNTIME_SERVICE_PROTOCOL_INCOMPATIBLE")
    return approved or generation_model(revision.model)


def binding(revision, endpoints, credential=None, trust_bytes=None):
    endpoint = endpoints.get(revision.endpoint_ref)
    root = Path(__file__).parent
    if isinstance(endpoint, local_bge.LocalBgeEndpoint):
        if (revision.endpoint_ref, revision.adapter, revision.model) != (local_bge.ENDPOINT_REF, local_bge.ADAPTER, local_bge.MODEL):
            raise ProviderError("SERVICE_MODEL_NOT_ADMITTED")
        artifacts = {name: digest((root / name).read_bytes()) for name in
                     ("local_bge.py", "inference_adapters.py", "service_use.py", "embedding_jobs.py")}
        return digest([revision.configuration_hash, local_bge.profile(), artifacts])
    if not isinstance(endpoint, LocalEndpoint) or revision.adapter == local_bge.ADAPTER:
        raise ProviderError("SERVICE_ENDPOINT_NOT_ADMITTED")
    if revision.model not in endpoint.allowed_models:
        raise ProviderError("SERVICE_MODEL_NOT_ADMITTED")
    if endpoint.egress == GENERATION_EGRESS:
        generation_profile(revision, endpoint, explicit=True)
    artifacts = {name: digest((root / name).read_bytes()) for name in
        ("service_admission.py", "provider_transport.py", "inference_adapters.py", "parsing/mineru.py",
         "service_use.py", "service_use_models.py", "parse_jobs.py", "embedding_jobs.py",
         "runtime_gate.py", "runtime_models.py", "answer_delivery.py", "worker.py")}
    return digest([revision.configuration_hash, endpoint.binding(credential, trust_bytes), artifacts])


def load_local_endpoints():
    """Deployment-only opt in; there is no environment default endpoint."""
    try:
        result = {}
        for env_key, egress in (("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", LOOPBACK_EGRESS),
                                ("DATACENTER_GENERATION_SERVICE_ENDPOINTS", GENERATION_EGRESS)):
            raw = os.environ.get(env_key, "{}")
            values = json.loads(raw) if len(raw) <= 8192 else None
            if not isinstance(values, dict) or len(values) > 10:
                raise ValueError()
            for ref, value in values.items():
                if ref in result or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ref) or not isinstance(value, dict):
                    raise ValueError()
                endpoint = LocalEndpoint(egress=egress, **value)
                endpoint.binding()
                result[ref] = endpoint
        if BAILIAN_ENDPOINT_REF in result or local_bge.ENDPOINT_REF in result:
            raise ValueError()
        if raw := os.environ.get(BAILIAN_ENV):
            value = json.loads(raw) if len(raw) <= 8192 else None
            if not isinstance(value, dict):
                raise ValueError()
            endpoint = bailian_endpoint(**value)
            endpoint.binding()
            result[BAILIAN_ENDPOINT_REF] = endpoint
        if endpoint := local_bge.read_model_endpoint():
            result[local_bge.ENDPOINT_REF] = endpoint
        return result
    except (ValueError, TypeError, ProviderError):
        raise ProviderError("SERVICE_ENDPOINT_CONFIGURATION_INVALID") from None


def _active_actor(db, actor_id):
    actor = db.scalar(select(User).where(User.id == actor_id).with_for_update(read=True).execution_options(populate_existing=True))
    if not actor or actor.status != "active" or not has_permission(actor, "system.configure"):
        raise ProviderError("SERVICE_ACTOR_REVOKED")


def revision_for(db, profile_id, revision_id):
    row = db.get(ServiceRevision, revision_id)
    if not row or row.profile_id != profile_id:
        raise SafeError("NOT_FOUND", "服务修订不存在", 404)
    return row


def create_revision(db, profile, endpoint_ref, adapter, model, endpoints):
    local_embedding = profile.kind == "embedding" and adapter == local_bge.ADAPTER and model == local_bge.MODEL
    if ((adapter != ADAPTERS.get(profile.kind) and not local_embedding) or not model or len(model) > 100
            or profile.kind == "embedding" and model not in {"BAAI/bge-m3", "Pro/BAAI/bge-m3"}
            or profile.kind == "parsing" and model != "vlm"):
        raise SafeError("SERVICE_CONFIGURATION_INVALID", "服务类型、适配器和模型不匹配", 422)
    config = {"endpoint_ref": endpoint_ref, "adapter": adapter, "model": model, "scope": "synthetic_only", "fixture": FIXTURE}
    if local_embedding:
        config["local_model"] = local_bge.profile()
    row = ServiceRevision(profile_id=profile.id, endpoint_ref=endpoint_ref, adapter=adapter,
                          model=model, configuration_hash=digest(config))
    binding(row, endpoints)  # Fail closed before saving an unusable endpoint.
    db.add(row)
    profile.revision += 1
    db.flush()
    return row


def new_test(db, revision, actor_id, endpoints):
    _active_actor(db, actor_id)
    row = ServiceTestRun(revision_id=revision.id, actor_id=actor_id, fixture_id=FIXTURE,
                         binding_hash=binding(revision, endpoints), state="pending")
    db.add(row)
    db.flush()
    return row


class AdmissionTransport:
    def __init__(self, db, test, endpoints, control_revision, authority_check=None):
        self.db, self.test, self.endpoints = db, test, endpoints
        self.control_revision, self.authority_check = control_revision, authority_check
        self.attempt = test.attempt

    def check(self):
        db, test = self.db, self.test
        db.refresh(test)
        revision = db.get(ServiceRevision, test.revision_id)
        profile = db.scalar(select(ServiceProfile).where(ServiceProfile.id == revision.profile_id)
            .with_for_update(read=True).execution_options(populate_existing=True))
        if self.authority_check:
            self.authority_check()
        _active_actor(db, test.actor_id)
        endpoint = self.endpoints.get(revision.endpoint_ref)
        if isinstance(endpoint, local_bge.LocalBgeEndpoint):
            credential = trust_bytes = tls_context = None
        elif isinstance(endpoint, LocalEndpoint):
            credential = endpoint.validate()
            trust_bytes, tls_context = endpoint.trust()
        else:
            raise ProviderError("SERVICE_ENDPOINT_NOT_ADMITTED")
        if (test.state != "running" or test.attempt != self.attempt or not future(test.lease_until) or profile.revision != self.control_revision
                or binding(revision, self.endpoints, credential, trust_bytes) != test.binding_hash):
            raise ProviderError("SERVICE_ADMISSION_STALE")
        claimed = db.execute(update(ServiceTestRun).where(ServiceTestRun.id == test.id,
            ServiceTestRun.state == "running", ServiceTestRun.attempt == self.attempt,
            ServiceTestRun.lease_until > utc_now()).values(attempt=ServiceTestRun.attempt)
            .execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise ProviderError("SERVICE_ADMISSION_STALE")
        return revision, endpoint, credential, tls_context

    def request(self, method, target, *, body=b"", purpose="api", content_type="application/json"):
        revision, endpoint, credential, tls_context = self.check()
        if not isinstance(endpoint, LocalEndpoint):
            raise ProviderError("SERVICE_DESTINATION_DENIED")
        if type(body) is not bytes or len(body) > 65536:
            raise ProviderError("SERVICE_REQUEST_BUDGET")
        # No proxy, URL credentials, fragments, alternate origin or redirects.
        try:
            parts = urlsplit(target)
            port = parts.port
        except ValueError:
            raise ProviderError("SERVICE_DESTINATION_DENIED") from None
        if parts.scheme or parts.netloc:
            if (parts.scheme != endpoint.scheme or parts.hostname != endpoint.hostname or port != endpoint.port
                    or parts.username is not None or parts.password is not None or parts.fragment):
                raise ProviderError("SERVICE_DESTINATION_DENIED")
            path = parts.path + ("?" + parts.query if parts.query else "")
        else:
            path = target
        allowed = ((purpose == "api" and method == "POST" and path in {"/v1/embeddings", endpoint.chat_path, "/api/v4/file-urls/batch"})
            or (purpose == "api" and method == "GET" and re.fullmatch(r"/api/v4/extract-results/batch/[A-Za-z0-9_.-]{1,128}", path))
            or (purpose == "upload" and method == "PUT" and parts.scheme == endpoint.scheme)
            or (purpose == "download" and method == "GET" and parts.scheme == endpoint.scheme))
        expected = {"generation": endpoint.chat_path, "embedding": "/v1/embeddings", "parsing": "/api/v4/file-urls/batch"}
        if (not allowed or not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n#\\")
                or method == "POST" and path != expected[self.db.get(ServiceProfile, revision.profile_id).kind]
                or endpoint.egress == GENERATION_EGRESS and (method, purpose, path) != ("POST", "api", expected["generation"])):
            raise ProviderError("SERVICE_DESTINATION_DENIED")
        count = self.db.scalar(select(func.count()).select_from(ServiceRequestCall).where(ServiceRequestCall.test_run_id == self.test.id))
        if count >= 12:
            raise ProviderError("SERVICE_CALL_BUDGET")
        call = ServiceRequestCall(test_run_id=self.test.id, ordinal=count + 1, method=method, purpose=purpose,
                                  target_hash=digest(target.encode()), payload_hash=digest(body), state="dispatching")
        self.db.add(call)
        self.db.commit()  # Durable intent before network; never retry an uncertain send.
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        if purpose == "api":
            headers["Authorization"] = "Bearer " + credential
        connection = endpoint.connect(8 if isinstance(endpoint, BailianEndpoint) else 2, tls_context)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(1048577)
            if len(raw) <= 1048576 and response.length:
                raise http.client.IncompleteRead(raw, response.length)
            call.response_status = response.status
            if len(raw) > 1048576:
                call.state, call.safe_error = "failed", "SERVICE_RESPONSE_BUDGET"
            elif not 200 <= response.status < 300:
                call.state, call.safe_error = "failed", "SERVICE_UPSTREAM_REJECTED"
            else:
                call.state, call.response_hash = "received", digest(raw)
            self.db.commit()
        except (OSError, http.client.HTTPException, UnicodeError):
            self.db.rollback()
            self.db.refresh(call)
            call.state, call.safe_error = "unknown", "SERVICE_OUTCOME_UNKNOWN"
            self.db.commit()
        finally:
            connection.close()
        self.check()  # Revocation withholds returned bytes; cannot unsend them.
        if call.safe_error:
            raise ProviderError(call.safe_error)
        return raw


def synthetic_pdf():
    # A deterministic one-page fixture; no optional PDF engine dependency.
    stream = b"BT /F1 10 Tf 40 740 Td (SYNTHETIC ONLY - DEMO-1. Disconnect power before maintenance.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    body, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for n, item in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{n} 0 obj\n".encode() + item + b"\nendobj\n")
    start = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        body.extend(f"{offset:010} 00000 n \n".encode())
    body.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(body)


def _bailian_test_result(transport):
    from .answer_delivery import _response_tools
    from .runtime_gate import _complete_stream, _provider_usage

    tool_name = "record_probe"
    body = {"model": BAILIAN_MODEL, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": 128, **BAILIAN_OPTIONS,
            "messages": [{"role": "user", "content": "Synthetic self-test only. Call record_probe with value 7."}],
            "tools": [{"type": "function", "function": {"name": tool_name, "description": "Record an integer.",
                       "parameters": {"type": "object", "properties": {"value": {"type": "integer"}},
                                      "required": ["value"], "additionalProperties": False}}}],
            "tool_choice": {"type": "function", "function": {"name": tool_name}}}
    raw = transport.request("POST", BAILIAN_CHAT_PATH, body=json.dumps(body).encode())
    if not _complete_stream(raw, expected_model=BAILIAN_MODEL, expected_finish="tool_calls"):
        raise ProviderError("SERVICE_FIXTURE_RESULT_MISMATCH")
    try:
        calls = _response_tools(raw.decode("utf-8"))
    except UnicodeError:
        raise ProviderError("SERVICE_FIXTURE_RESULT_MISMATCH") from None
    if (len(calls) != 1 or not calls[0]["id"] or calls[0]["name"] != tool_name
            or calls[0]["args"] != {"value": 7} or type(calls[0]["args"]["value"]) is not int):
        raise ProviderError("SERVICE_FIXTURE_RESULT_MISMATCH")
    return {"model": BAILIAN_MODEL, "usage": _provider_usage(raw), "tool_call_verified": True}


def run_test(db: Session, test_id: str, endpoints: dict, *, authority_check=None):
    test = db.get(ServiceTestRun, test_id)
    revision = db.get(ServiceRevision, test.revision_id)
    profile = db.get(ServiceProfile, revision.profile_id, populate_existing=True)
    _active_actor(db, test.actor_id)
    if test.state not in {"pending", "awaiting_remote"}:
        return test  # Includes unknown: neither a retry nor an invented success.
    if binding(revision, endpoints) != test.binding_hash:
        raise ProviderError("SERVICE_ADMISSION_STALE")
    claimed = db.execute(update(ServiceTestRun).where(ServiceTestRun.id == test.id, ServiceTestRun.state == test.state)
                         .values(state="running", attempt=ServiceTestRun.attempt + 1, lease_until=utc_now() + timedelta(seconds=120))
                         .execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.rollback()
        raise ProviderError("SERVICE_TEST_IN_PROGRESS")
    db.commit()
    db.refresh(test)
    transport = AdmissionTransport(db, test, endpoints, profile.revision, authority_check)
    try:
        if profile.kind == "generation" and isinstance(endpoints.get(revision.endpoint_ref), BailianEndpoint):
            summary = _bailian_test_result(transport)
        elif profile.kind == "generation":
            from .inference_adapters import ChatAdapter
            result = ChatAdapter(transport, revision.model).complete([{"role": "user", "content": "SYNTHETIC fixture: reply SYNTHETIC OK."}])
            if result["text"].strip() != "SYNTHETIC OK":
                raise ProviderError("SERVICE_FIXTURE_RESULT_MISMATCH")
            summary = {"model": result["model"], "usage": result["usage"], "text_present": bool(result["text"])}
        elif profile.kind == "embedding":
            from .inference_adapters import BgeM3Adapter
            texts = ["SYNTHETIC DEMO-1 power on", "SYNTHETIC DEMO-1 maintenance warning"]
            if revision.adapter == local_bge.ADAPTER:
                _, endpoint, *_ = transport.check()
                db.commit()  # Let policy disable/authority revoke while CPU inference runs.
                result = endpoint.embed(texts)
            else:
                result = BgeM3Adapter(transport, revision.model).embed(texts)
            summary = {"model": result["model"], "usage": result["usage"], "count": len(result["vectors"]), "dimensions": 1024}
            if revision.adapter == local_bge.ADAPTER:
                summary.update(local_model=local_bge.profile(), result_sha256=digest(result["vectors"]))
        else:
            from .parsing.mineru import MinerUAdapter
            adapter = MinerUAdapter(transport)
            def checkpoint(batch_id):
                transport.check()
                changed = db.execute(update(ServiceTestRun).where(ServiceTestRun.id == test.id,
                    ServiceTestRun.state == "running", ServiceTestRun.attempt == transport.attempt,
                    ServiceTestRun.lease_until > utc_now()).values(checkpoint={"batch_id": batch_id, "data_id": test.id})
                    .execution_options(synchronize_session=False))
                if changed.rowcount != 1:
                    raise ProviderError("SERVICE_ADMISSION_STALE")
                db.commit()
                db.refresh(test)
            batch_id = test.checkpoint.get("batch_id")
            if not batch_id:
                batch_id = adapter.submit(synthetic_pdf(), test.id, checkpoint=checkpoint)
            blocks = adapter.poll(batch_id, test.id)
            if blocks is None:
                transport.check()
                db.execute(update(ServiceTestRun).where(ServiceTestRun.id == test.id, ServiceTestRun.attempt == transport.attempt,
                    ServiceTestRun.state == "running").values(state="awaiting_remote").execution_options(synchronize_session=False))
                db.commit()
                db.refresh(test)
                return test
            joined = " ".join(" ".join(x[0].split()) for x in blocks)
            if ("SYNTHETIC" not in joined or "Disconnect power before maintenance." not in joined
                    or any(x[1].get("file_page") != 1 or x[1].get("needs_review") is not True for x in blocks)):
                raise ProviderError("SERVICE_FIXTURE_EVIDENCE_MISSING")
            summary = {"block_count": len(blocks), "file_pages": sorted({x[1]["file_page"] for x in blocks}),
                       "review_state": "unreviewed", "provider_batch_id": batch_id}
        transport.check()
        db.execute(update(ServiceTestRun).where(ServiceTestRun.id == test.id, ServiceTestRun.attempt == transport.attempt,
            ServiceTestRun.state == "running").values(state="succeeded", result=summary, completed_at=utc_now(),
            valid_until=utc_now() + timedelta(hours=1)).execution_options(synchronize_session=False))
    except (ProviderError, SafeError) as exc:
        db.rollback()
        db.refresh(test)
        if test.attempt != transport.attempt or test.state != "running":
            return test
        code = exc.code
        db.execute(update(ServiceTestRun).where(ServiceTestRun.id == test.id, ServiceTestRun.attempt == transport.attempt,
            ServiceTestRun.state == "running").values(safe_error=code, state="unknown" if code == "SERVICE_OUTCOME_UNKNOWN" else "failed")
            .execution_options(synchronize_session=False))
    db.commit()
    db.refresh(test)
    return test


def prepare_resume(db, test):
    """Recover existing remote identity only. Never repeat POST or upload."""
    changed = db.execute(update(ServiceTestRun).where(ServiceTestRun.id == test.id, ServiceTestRun.state == test.state,
        ServiceTestRun.attempt == test.attempt).values(attempt=ServiceTestRun.attempt).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise SafeError("SERVICE_TEST_IN_PROGRESS", "服务测试状态已变化", 409)
    db.refresh(test)
    if test.state == "pending" and test.attempt == 0:
        if db.scalar(select(func.count()).select_from(ServiceRequestCall).where(ServiceRequestCall.test_run_id == test.id)) == 0:
            return  # Accepted receipt committed before the first trusted claim.
    if test.state == "running" and future(test.lease_until):
        raise SafeError("SERVICE_TEST_IN_PROGRESS", "服务自检仍持有租约", 409)
    if test.state not in {"awaiting_remote", "unknown", "running"}:
        raise SafeError("SERVICE_RECONCILIATION_REQUIRED", "无可核对的远端标识，禁止重复提交", 409)
    for call in db.scalars(select(ServiceRequestCall).where(ServiceRequestCall.test_run_id == test.id, ServiceRequestCall.state == "dispatching")).all():
        call.state, call.safe_error = "unknown", "SERVICE_OUTCOME_UNKNOWN"
    test.state = "awaiting_remote" if test.checkpoint.get("batch_id") else "unknown"
    test.safe_error = None if test.checkpoint.get("batch_id") else "SERVICE_RECONCILIATION_REQUIRED"
    test.attempt += 1
    db.flush()


def preview_change(db, profile, actor_id, action, revision_id, endpoints):
    test, bound = None, None
    if action == "activate":
        revision = revision_for(db, profile.id, revision_id)
        bound = binding(revision, endpoints)
        test = db.scalar(select(ServiceTestRun).where(ServiceTestRun.revision_id == revision.id,
            ServiceTestRun.binding_hash == bound, ServiceTestRun.state == "succeeded").order_by(ServiceTestRun.completed_at.desc()))
        if not test or not future(test.valid_until):
            raise SafeError("SERVICE_TEST_REQUIRED", "需要当前配置的有效合成自检", 409)
    row = ServiceChangePreview(profile_id=profile.id, actor_id=actor_id, action=action, revision_id=revision_id,
        profile_revision=profile.revision, binding_hash=bound, test_run_id=test.id if test else None,
        expires_at=utc_now() + timedelta(minutes=5))
    db.add(row)
    db.flush()
    return row


def apply_change(db, profile, actor_id, action, preview_id, endpoints):
    preview = db.get(ServiceChangePreview, preview_id)
    if (not preview or preview.profile_id != profile.id or preview.actor_id != actor_id or preview.action != action
            or preview.consumed or preview.profile_revision != profile.revision or not future(preview.expires_at)):
        raise SafeError("SERVICE_PREVIEW_STALE", "预检已失效，请刷新", 409)
    if action == "activate":
        revision = revision_for(db, profile.id, preview.revision_id)
        test = db.get(ServiceTestRun, preview.test_run_id)
        if (binding(revision, endpoints) != preview.binding_hash or not test or test.state != "succeeded"
                or not future(test.valid_until) or test.binding_hash != preview.binding_hash):
            raise SafeError("SERVICE_PREVIEW_STALE", "配置或准入已变化", 409)
        profile.active_revision_id, profile.enabled = revision.id, True
    else:
        profile.enabled = False  # Safety stop does not depend on a functioning provider/secret.
        profile.use_epoch += 1  # Old business pins stay revoked after re-enable.
    preview.consumed = True
    profile.revision += 1
    db.flush()
