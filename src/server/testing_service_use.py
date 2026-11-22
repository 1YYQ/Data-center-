"""Private fixture issuer. Never exposed through HTTP or treated as real approval.

Callers own synthetic sample construction. A label supplied by a browser is not
provenance: production business paths only consume these exact-byte records.
"""
from datetime import timedelta
import re

from sqlalchemy import select

from .models import utc_now
from .service_admission import digest
from .service_use_models import SyntheticServiceInput


def _fixture_inputs_match(task, approved_inputs, approved_effective):
    """Permit only transport metadata and a server-resolved fixed model to vary.

    Conversation IDs/revisions are not business content. They must still be
    canonical and point to this accepted task's conversation. Every other field
    retains the exact fixed-fixture comparison, including unexpected keys.
    """
    from uuid import UUID
    original, effective = dict(task.input_snapshot), dict(task.effective_input)
    conversation = original.get("conversation_id")
    revision = original.get("context_revision")
    if any(original.get(key) != effective.get(key) for key in ("conversation_id", "context_revision")):
        return False
    if conversation is None:
        if revision is not None:
            return False
    else:
        try:
            if str(UUID(conversation)) != conversation or conversation != task.conversation_id:
                return False
        except (ValueError, TypeError, AttributeError):
            return False
        if type(revision) is not int or revision < 0:
            return False
    for payload in (original, effective):
        payload["conversation_id"], payload["context_revision"] = None, None
    if effective not in approved_effective:
        return False
    # Only the model resolved and validated on the accepted task is inherited;
    # no supplied unknown model or question is silently rewritten.
    if original.get("model") is None:
        original["model"] = effective["model"]
    return original in approved_inputs


def attest_synthetic_input(db, actor_id, purpose, resource_key, payload):
    if type(payload) is not bytes or not resource_key:
        raise ValueError("Exact synthetic bytes and a resource identity are required")
    row = SyntheticServiceInput(actor_id=actor_id, purpose=purpose, resource_key=resource_key,
        input_hash=digest(payload), provenance="private-local-synthetic-fixture-v1",
        expires_at=utc_now() + timedelta(hours=1), revoked=False)
    db.add(row)
    db.flush()
    return row


def install_synthetic_answer_fixture(app, *, actor_id, knowledge_base_id, version_id, parse_run_id,
                                     source_sha256, query, model, allowed_modes=("answer",)):
    """Opt-in issuer for one fixed synthetic fixture in an isolated pilot DB.

    The normal private route still verifies and replays the resulting exact
    proof. Neither public query parameters nor a runtime request body can
    choose the question, scope, source identity or bytes approved here.
    """
    from starlette.concurrency import run_in_threadpool
    from sqlalchemy.engine import make_url
    from .governance import publication_is_current
    from .models import ChunkSet, Document, DocumentVersion, FileObject, ParseRun
    from .execution_models import Conversation, ConversationMessage
    from .parsing.derivation import _verify_original
    from .parsing.local_pipeline import ParseContractError
    from .runtime_api import _credential
    from .runtime_gate import _assemble, _task_service, _validate_run
    from .schemas import QueryInput
    from .service_admission import BAILIAN_WIRE
    from .services import SafeError

    config = app.state.settings
    database = make_url(config.database_url)
    if (database.drivername != "postgresql+psycopg" or database.host != "127.0.0.1"
            or not (database.database or "").startswith("dc_local_") or database.query
            or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)):
        raise ValueError("Synthetic answer fixture requires an isolated loopback PostgreSQL database and source hash")

    if (not isinstance(allowed_modes, tuple) or not allowed_modes
            or len(set(allowed_modes)) != len(allowed_modes)
            or any(mode not in {"retrieve", "answer"} for mode in allowed_modes)):
        raise ValueError("Only explicitly configured synthetic query modes are allowed")
    approved_effective = [QueryInput(query=query, model=model, knowledge_base_ids=[knowledge_base_id],
                          mode=mode, service_mode="selected_service").model_dump() for mode in allowed_modes]
    approved_inputs = approved_effective + [{**value, "knowledge_base_ids": []} for value in approved_effective]
    route = re.compile(r"/(?:api/v1/)?private/runtime/runs/([0-9a-f-]{36})/context-project")

    def issue(run_id, token):
        with app.state.session_factory() as db:
            run, task, actor, profile = _validate_run(db, run_id, token)
            if (actor.id != actor_id or not _fixture_inputs_match(task, approved_inputs, approved_effective)
                    or task.input_snapshot.get("mode") != task.effective_input.get("mode")
                    or task.accepted_scope.get("knowledge_base_ids") != [knowledge_base_id]
                    or profile.configuration["generation"].get("wire_protocol") != BAILIAN_WIRE):
                return
            conversation = db.get(Conversation, task.conversation_id)
            message = db.get(ConversationMessage, task.source_message_id)
            if (not conversation or conversation.owner_user_id != actor_id or conversation.status != "active"
                    or not message or message.conversation_id != conversation.id or message.author_user_id != actor_id
                    or message.role != "user" or message.purpose != "query" or message.content != query
                    or (task.input_snapshot.get("context_revision") is not None
                        and task.input_snapshot["context_revision"] > conversation.revision)):
                return
            service = _task_service(db, task, "answer", profile)
            if service is None or service[-1].get("wire_protocol") != BAILIAN_WIRE:
                return
            version = db.get(DocumentVersion, version_id)
            document = db.get(Document, version.document_id) if version else None
            file = db.get(FileObject, version.source_file_id) if version else None
            chunks = db.get(ChunkSet, version.selected_chunk_set_id) if version and version.selected_chunk_set_id else None
            parse = db.get(ParseRun, parse_run_id)
            if (not all((version, document, file, chunks, parse)) or document.knowledge_base_id != knowledge_base_id
                    or model not in version.model_scope or chunks.parse_run_id != parse.id
                    or parse.document_version_id != version.id or parse.status != "succeeded"
                    or file.sha256 != source_sha256 or file.storage_name != "local" or file.status != "available"
                    or not publication_is_current(db, version, historical=False)):
                return
            _verify_original(file, config)
            context, dependencies = _assemble(db, run, task, actor, profile, "answer", "answer")
            fragments = [source.get("fragment", {}) for source in dependencies["sources"]]
            if any(fragment.get("document_version_id") != version_id or fragment.get("parse_run_id") != parse_run_id
                    or fragment.get("locator", {}).get("source_sha256") != source_sha256 for fragment in fragments):
                return
            payload = context.encode("utf-8")
            pin = service[0]
            prior = db.scalar(select(SyntheticServiceInput.id).where(SyntheticServiceInput.actor_id == actor_id,
                SyntheticServiceInput.purpose == pin.purpose, SyntheticServiceInput.resource_key == pin.resource_key,
                SyntheticServiceInput.input_hash == digest(payload)))
            if prior is None:
                attest_synthetic_input(db, actor_id, pin.purpose, pin.resource_key, payload)
                db.commit()

    @app.middleware("http")
    async def attest_fixture_projection(request, call_next):
        match = route.fullmatch(request.url.path)
        if match and request.method == "POST":
            try:
                await run_in_threadpool(issue, match[1], _credential(request))
            except (SafeError, ParseContractError):
                pass  # The normal route retains the authority check and error response.
        return await call_next(request)
