from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db import build_engine, init_db
from .models import AuthorizationGrant, BusinessApproval, ChunkSet, ContentBlock, Document, DocumentVersion, KnowledgeBase, QueryTask, TaskEvidence, TaskOutbox, TaskStep, User, utc_now
from .security import hash_password, verify_password
from .services import SafeError, build_query_result, can_access_document, create_index, evidence_is_readable, legacy_index_is_current, make_parse_run, process_query_task, save_upload, search_blocks, seed_reference_data, split_terms, switch_legacy_index
from .testing_governance import seed_verified_approval, seed_verified_version


@pytest.fixture()
def fixture_db():
    root = Path("outputs") / "test-fixtures" / ("device-query-" + uuid.uuid4().hex)
    root.mkdir(parents=True)
    config = Settings(database_url="sqlite:///./" + (root / "db.sqlite").as_posix(), storage_dir=root / "storage", bootstrap_token="test")
    engine = build_engine(config.database_url)
    init_db(engine, config, migrate=True)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as db:
        seed_reference_data(db)
        yield db, config
    shutil.rmtree(root, ignore_errors=True)


def _activate_fixture_index(db: Session, kb: KnowledgeBase, actor: User):
    generation = create_index(db, kb, actor)
    assert generation.status == "ready", generation.build_report["integrity"]
    return switch_legacy_index(db, kb, generation.id, actor,
        expected_active_id=kb.active_generation_id, expected_source_revision=kb.content_revision)


def _ready_fixture(db: Session, config: Settings):
    manager = User(display_name="Manager", username="manager-" + uuid.uuid4().hex, status="active", credential_state="active", credential_hash=hash_password("manager password"), roles=["knowledge_manager"])
    db.add(manager)
    db.flush()
    kb = KnowledgeBase(name="KB-" + uuid.uuid4().hex)
    db.add(kb)
    db.flush()
    for operation in ("knowledge.manage", "document.read", "knowledge.query"):
        approval = BusinessApproval(applicant=manager.id, approver=manager.id, approved_at=utc_now(), scope={"knowledge_base_id": kb.id, "document_id": None, "level": "test-level", "category": "test-category", "need_class": "test-need"}, operations=[operation], status="approved", verification_state="verified")
        db.add(approval)
        db.flush()
        seed_verified_approval(db, approval)
        db.add(AuthorizationGrant(subject_user_id=manager.id, knowledge_base_id=kb.id, operation=operation, level="test-level", category="test-category", need_class="test-need", approval_id=approval.id, granted_by=manager.id, valid_until=approval.valid_until))
    file_object = save_upload([b"%PDF-1.4 local test"], "manual.pdf", "application/pdf", manager, config)
    db.add(file_object)
    db.flush()
    # Explicit synthetic dimensions satisfy the production authorization tuple.
    document = Document(knowledge_base_id=kb.id, title="Demo Manual", access_level="test-level", category="test-category", need_class="test-need")
    db.add(document)
    db.flush()
    version = DocumentVersion(document_id=document.id, version_no=1, model_scope=["DEMO-1"], source_file_id=file_object.id, created_by=manager.id)
    db.add(version)
    db.commit()
    parse_run = make_parse_run(db, version, file_object, config)
    db.commit()
    block = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == parse_run.id)).first()
    block.text_content = "DEMO-1 开机步骤：连接电源并检查状态灯。"
    block.locator = {"section": "SYNTHETIC controlled test excerpt", "ordinal": 1}
    block.review_status = "approved"
    parse_run.status = "succeeded"
    seed_verified_version(db, manager, document, version, parse_run, db.scalar(select(ChunkSet.id).where(ChunkSet.parse_run_id == parse_run.id)))
    db.commit()
    _activate_fixture_index(db, kb, manager)
    db.commit()
    return manager, kb, document, version, block


def test_legacy_index_build_and_switch_are_separate_and_rollback_rechecks_revision(fixture_db):
    db, config = fixture_db
    actor, kb, *_ = _ready_fixture(db, config)
    previous = kb.active_generation_id
    candidate = create_index(db, kb, actor)
    assert candidate.status == "ready" and kb.active_generation_id == previous
    assert legacy_index_is_current(db, kb, candidate)
    candidate.status = "ready_partial"
    with pytest.raises(SafeError) as denied:
        switch_legacy_index(db, kb, candidate.id, actor,
            expected_active_id=previous, expected_source_revision=kb.content_revision)
    assert denied.value.code == "INDEX_GENERATION_NOT_READY" and kb.active_generation_id == previous
    candidate.status = "ready"
    switch_legacy_index(db, kb, candidate.id, actor,
        expected_active_id=previous, expected_source_revision=kb.content_revision)
    db.commit()
    assert kb.active_generation_id == candidate.id
    switch_legacy_index(db, kb, previous, actor, expected_active_id=candidate.id,
        expected_source_revision=kb.content_revision, action="rollback")
    db.commit()
    kb.content_revision += 1
    db.commit()
    with pytest.raises(SafeError) as stale:
        switch_legacy_index(db, kb, candidate.id, actor,
            expected_active_id=previous, expected_source_revision=kb.content_revision, action="rollback")
    assert stale.value.code == "INDEX_GENERATION_NOT_READY" and kb.active_generation_id == previous


def test_legacy_index_keeps_unqualified_published_source_and_refuses_semantic_fallback(fixture_db):
    db, config = fixture_db
    actor, kb, doc, version, _ = _ready_fixture(db, config)
    previous = kb.active_generation_id
    other_doc = Document(knowledge_base_id=kb.id, title="SYNTHETIC unqualified publication", access_level=doc.access_level,
                         category=doc.category, need_class=doc.need_class)
    db.add(other_doc)
    db.flush()
    unqualified = DocumentVersion(document_id=other_doc.id, version_no=1, source_file_id=version.source_file_id,
        publish_status="effective", created_by=actor.id)
    db.add(unqualified)
    db.flush()
    generation = create_index(db, kb, actor)
    assert generation.status == "failed" and kb.active_generation_id == previous
    sources = generation.build_report["manifest"]["expected_sources"]
    assert {item["version_id"] for item in sources} == {version.id, unqualified.id}
    assert generation.build_report["integrity"]["blockers"] == [
        {"version_id": unqualified.id, "reason": "selected_source_not_ready"}]
    unqualified.publish_status = "withdrawn"
    db.get(ChunkSet, version.selected_chunk_set_id).config_hash = "a" * 64
    db.flush()
    refused = create_index(db, kb, actor)
    assert refused.status == "failed" and refused.build_report["integrity"]["blockers"] == [
        {"version_id": version.id, "reason": "semantic_source_requires_child_index"}]
    assert kb.active_generation_id == previous


def test_password_digest_is_verifiable_without_plaintext(fixture_db):
    db, _ = fixture_db
    user = User(display_name="A", username="a-" + uuid.uuid4().hex, status="active", credential_hash=hash_password("a sufficiently long password"), roles=["employee"])
    assert verify_password("a sufficiently long password", user.credential_hash)
    assert not verify_password("wrong password", user.credential_hash)


def test_chinese_keyword_terms_overlap_with_long_passages():
    passage = "资料登记信息。" * 15 + "按下电源开关后指示灯亮起。"
    assert "电源" in split_terms("如何开启电源？") & split_terms(passage)
    assert "源：" not in split_terms("电源：开关")


def test_query_result_keeps_evidence_and_requires_permissions(fixture_db):
    db, config = fixture_db
    manager, kb, document, version, _ = _ready_fixture(db, config)
    task = QueryTask(owner_user_id=manager.id, kind="query", input_snapshot={"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]}, idempotency_key="test-query", status="running")
    db.add(task)
    db.flush()
    build_query_result(db, task, manager)
    db.commit()
    assert task.status == "succeeded"
    assert task.result_summary["result_status"] == "formal"
    assert len(db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == task.id)).all()) == 1
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == manager.id, AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = utc_now()
    db.commit()
    assert not can_access_document(db, manager, document)


def test_version_conflict_reason_requires_current_access_and_matching_scope(fixture_db):
    from .governance import create_hold

    db, config = fixture_db
    manager, kb, _, version, _ = _ready_fixture(db, config)
    create_hold(db, manager, version, {"kind": "conflict", "reason": "internal conflicting approval"})
    db.commit()
    assert search_blocks(db, manager, "开机步骤", "DEMO-1", [kb.id]) == ([], ["version_conflict"])
    assert "version_conflict" not in search_blocks(db, manager, "开机步骤", "OTHER", [kb.id])[1]
    assert "version_conflict" not in search_blocks(db, manager, "开机步骤", "DEMO-1", ["unrelated"])[1]
    grant = db.scalar(select(AuthorizationGrant).where(
        AuthorizationGrant.subject_user_id == manager.id, AuthorizationGrant.operation == "knowledge.query"))
    grant.revoked_at = utc_now()
    db.commit()
    assert "version_conflict" not in search_blocks(db, manager, "开机步骤", "DEMO-1", [kb.id])[1]


def test_parse_upload_is_not_publishable_until_block_review(fixture_db):
    db, config = fixture_db
    manager, kb, document, version, block = _ready_fixture(db, config)
    block.review_status = "unreviewed"
    version.content_review_status = "approved"
    version.applicability_status = "confirmed"
    db.commit()
    task = QueryTask(owner_user_id=manager.id, kind="query", input_snapshot={"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]}, idempotency_key="pending-review", status="running")
    db.add(task)
    db.flush()
    build_query_result(db, task, manager)
    assert task.result_summary["result_status"] == "insufficient_evidence"
    assert "evidence_pending_review" in task.warnings


def test_upload_size_failure_cleans_temporary_file(fixture_db):
    db, config = fixture_db
    manager = User(display_name="Manager", username="size-" + uuid.uuid4().hex, status="active", roles=["knowledge_manager"])
    with pytest.raises(SafeError) as error:
        save_upload([b"1234"], "too-large.pdf", "application/pdf", manager, Settings(database_url=config.database_url, storage_dir=config.storage_dir, max_upload_bytes=3))
    assert error.value.status_code == 413
    assert not list(config.storage_dir.glob(".upload-*.tmp"))


def test_pending_query_recovers_from_persisted_step(fixture_db):
    db, config = fixture_db
    manager, kb, _, _, _ = _ready_fixture(db, config)
    task = QueryTask(owner_user_id=manager.id, input_snapshot={"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]}, idempotency_key="pending-recovery", status="pending")
    db.add(task)
    db.flush()
    step = TaskStep(task_id=task.id, step_key="query", position=1, kind="knowledge_query", status="pending")
    db.add(step)
    db.flush()
    db.add(TaskOutbox(step_id=step.id, dispatch_no=1, status="pending"))
    db.commit()
    process_query_task(db, task, manager)
    db.commit()
    assert task.status == "succeeded"
    assert step.status == "succeeded"


def test_stale_index_does_not_answer_from_new_content(fixture_db):
    db, config = fixture_db
    manager, kb, _, _, _ = _ready_fixture(db, config)
    kb.content_revision += 1
    task = QueryTask(owner_user_id=manager.id, input_snapshot={"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]}, idempotency_key="stale-index", status="running")
    db.add(task)
    db.flush()
    build_query_result(db, task, manager)
    assert task.result_summary["result_status"] == "insufficient_evidence"
    assert "index_stale" in task.warnings


def test_withdrawn_version_invalidates_saved_evidence(fixture_db):
    db, config = fixture_db
    manager, kb, _, version, _ = _ready_fixture(db, config)
    task = QueryTask(owner_user_id=manager.id, input_snapshot={"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]}, idempotency_key="withdrawn-evidence", status="running")
    db.add(task)
    db.flush()
    build_query_result(db, task, manager)
    db.flush()
    evidence = db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task.id))
    version.publish_status = "withdrawn"
    db.commit()
    assert evidence is not None
    assert not evidence_is_readable(db, manager, evidence)
