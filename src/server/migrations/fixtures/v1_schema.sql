-- Frozen v1 SQLite DDL; no business data.
-- Source: outputs/first-release-baseline/2026-11-17/code-phase/before/src/server/models.py
-- Source SHA256: b2ae05195fd37957a8e08883c9374c6a8d1258800f591974645997b75fb24ccf
-- Generated from that snapshot in an independent module and in-memory SQLite.

CREATE TABLE api_idempotency_records (
	id VARCHAR(36) NOT NULL, 
	actor_user_id VARCHAR(36) NOT NULL, 
	idempotency_key VARCHAR(200) NOT NULL, 
	method VARCHAR(10) NOT NULL, 
	request_path VARCHAR(300) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	resource_refs JSON NOT NULL, 
	response_status INTEGER, 
	error_code VARCHAR(100), 
	PRIMARY KEY (id), 
	UNIQUE (actor_user_id, idempotency_key), 
	FOREIGN KEY(actor_user_id) REFERENCES users (id)
);

CREATE TABLE audit_events (
	id VARCHAR(36) NOT NULL, 
	actor_user_id VARCHAR(36), 
	action VARCHAR(120) NOT NULL, 
	resource_type VARCHAR(80) NOT NULL, 
	resource_id VARCHAR(36), 
	reason VARCHAR(500), 
	metadata_json JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(actor_user_id) REFERENCES users (id)
);

CREATE TABLE auth_sessions (
	id VARCHAR(36) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	token_hash VARCHAR(128) NOT NULL, 
	csrf_hash VARCHAR(128) NOT NULL, 
	issued_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	authz_revision INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE authorization_grants (
	id VARCHAR(36) NOT NULL, 
	subject_user_id VARCHAR(36) NOT NULL, 
	knowledge_base_id VARCHAR(36), 
	document_id VARCHAR(36), 
	operation VARCHAR(120) NOT NULL, 
	level VARCHAR(80), 
	category VARCHAR(80), 
	need_class VARCHAR(80), 
	valid_from DATETIME NOT NULL, 
	valid_until DATETIME, 
	revoked_at DATETIME, 
	approval_id VARCHAR(36), 
	granted_by VARCHAR(36), 
	PRIMARY KEY (id), 
	FOREIGN KEY(subject_user_id) REFERENCES users (id), 
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id), 
	FOREIGN KEY(approval_id) REFERENCES business_approvals (id), 
	FOREIGN KEY(granted_by) REFERENCES users (id)
);

CREATE TABLE business_approvals (
	id VARCHAR(36) NOT NULL, 
	applicant VARCHAR(36) NOT NULL, 
	approver VARCHAR(36) NOT NULL, 
	approved_at DATETIME, 
	scope JSON NOT NULL, 
	operations JSON NOT NULL, 
	valid_from DATETIME NOT NULL, 
	valid_until DATETIME, 
	evidence_locator VARCHAR(500), 
	status VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE chunk_sets (
	id VARCHAR(36) NOT NULL, 
	parse_run_id VARCHAR(36) NOT NULL, 
	splitter_version VARCHAR(60) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(parse_run_id) REFERENCES parse_runs (id)
);

CREATE TABLE content_blocks (
	id VARCHAR(36) NOT NULL, 
	parse_run_id VARCHAR(36) NOT NULL, 
	ordinal INTEGER NOT NULL, 
	kind VARCHAR(40) NOT NULL, 
	text_content TEXT NOT NULL, 
	structured_content JSON NOT NULL, 
	locator JSON NOT NULL, 
	review_status VARCHAR(20) NOT NULL, 
	review_reason VARCHAR(500), 
	PRIMARY KEY (id), 
	FOREIGN KEY(parse_run_id) REFERENCES parse_runs (id)
);

CREATE TABLE document_versions (
	id VARCHAR(36) NOT NULL, 
	document_id VARCHAR(36) NOT NULL, 
	version_no INTEGER NOT NULL, 
	revision_label VARCHAR(100), 
	model_scope JSON NOT NULL, 
	applicability_status VARCHAR(20) NOT NULL, 
	content_review_status VARCHAR(20) NOT NULL, 
	publish_status VARCHAR(20) NOT NULL, 
	source_file_id VARCHAR(36) NOT NULL, 
	effective_at DATETIME, 
	created_by VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (document_id, version_no), 
	FOREIGN KEY(document_id) REFERENCES documents (id), 
	FOREIGN KEY(source_file_id) REFERENCES file_objects (id), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE TABLE documents (
	id VARCHAR(36) NOT NULL, 
	knowledge_base_id VARCHAR(36) NOT NULL, 
	title VARCHAR(300) NOT NULL, 
	category VARCHAR(100), 
	access_level VARCHAR(80), 
	need_class VARCHAR(80), 
	access_mode VARCHAR(30) NOT NULL, 
	deleted_at DATETIME, 
	revision INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
);

CREATE TABLE file_objects (
	id VARCHAR(36) NOT NULL, 
	storage_name VARCHAR(80) NOT NULL, 
	object_key VARCHAR(500) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	byte_size INTEGER NOT NULL, 
	mime_type VARCHAR(120) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	created_by VARCHAR(36), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (object_key), 
	FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE TABLE index_generations (
	id VARCHAR(36) NOT NULL, 
	knowledge_base_id VARCHAR(36) NOT NULL, 
	embedding_profile_id VARCHAR(100) NOT NULL, 
	source_revision INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	expected_chunk_count INTEGER NOT NULL, 
	build_report JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	ready_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id)
);

CREATE TABLE index_sources (
	id VARCHAR(36) NOT NULL, 
	index_generation_id VARCHAR(36) NOT NULL, 
	document_version_id VARCHAR(36) NOT NULL, 
	parse_run_id VARCHAR(36) NOT NULL, 
	chunk_set_id VARCHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (index_generation_id, document_version_id), 
	FOREIGN KEY(index_generation_id) REFERENCES index_generations (id), 
	FOREIGN KEY(document_version_id) REFERENCES document_versions (id), 
	FOREIGN KEY(parse_run_id) REFERENCES parse_runs (id), 
	FOREIGN KEY(chunk_set_id) REFERENCES chunk_sets (id)
);

CREATE TABLE knowledge_bases (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	content_revision INTEGER NOT NULL, 
	acl_revision INTEGER NOT NULL, 
	active_generation_id VARCHAR(36), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE parse_runs (
	id VARCHAR(36) NOT NULL, 
	document_version_id VARCHAR(36) NOT NULL, 
	origin VARCHAR(30) NOT NULL, 
	provider_task_id VARCHAR(200), 
	status VARCHAR(30) NOT NULL, 
	output_revision VARCHAR(40) NOT NULL, 
	error_code VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_version_id) REFERENCES document_versions (id)
);

CREATE TABLE permissions (
	id VARCHAR(36) NOT NULL, 
	code VARCHAR(120) NOT NULL, 
	description VARCHAR(300) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
);

CREATE TABLE provider_calls (
	id VARCHAR(36) NOT NULL, 
	provider VARCHAR(100) NOT NULL, 
	operation VARCHAR(100) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	data_scope VARCHAR(200) NOT NULL, 
	cost_units FLOAT NOT NULL, 
	error_code VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE query_tasks (
	id VARCHAR(36) NOT NULL, 
	owner_user_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(40) NOT NULL, 
	input_snapshot JSON NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	next_action VARCHAR(200), 
	result_summary JSON NOT NULL, 
	warnings JSON NOT NULL, 
	error_code VARCHAR(100), 
	idempotency_key VARCHAR(200) NOT NULL, 
	revision INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (owner_user_id, idempotency_key), 
	FOREIGN KEY(owner_user_id) REFERENCES users (id)
);

CREATE TABLE roles (
	id VARCHAR(36) NOT NULL, 
	code VARCHAR(80) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (code)
);

CREATE TABLE task_artifacts (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(80) NOT NULL, 
	validation_status VARCHAR(30) NOT NULL, 
	file_object_id VARCHAR(36), 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES query_tasks (id), 
	FOREIGN KEY(file_object_id) REFERENCES file_objects (id)
);

CREATE TABLE task_attempts (
	id VARCHAR(36) NOT NULL, 
	step_id VARCHAR(36) NOT NULL, 
	attempt_no INTEGER NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	error_code VARCHAR(100), 
	diagnostic JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (step_id, attempt_no), 
	FOREIGN KEY(step_id) REFERENCES task_steps (id)
);

CREATE TABLE task_evidence (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	document_version_id VARCHAR(36) NOT NULL, 
	block_id VARCHAR(36) NOT NULL, 
	quote_snapshot TEXT NOT NULL, 
	locator JSON NOT NULL, 
	review_at_run VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(task_id) REFERENCES query_tasks (id), 
	FOREIGN KEY(document_version_id) REFERENCES document_versions (id), 
	FOREIGN KEY(block_id) REFERENCES content_blocks (id)
);

CREATE TABLE task_outbox (
	id VARCHAR(36) NOT NULL, 
	step_id VARCHAR(36) NOT NULL, 
	dispatch_no INTEGER NOT NULL, 
	event_type VARCHAR(80) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	available_at DATETIME NOT NULL, 
	published_at DATETIME, 
	publish_attempts INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (step_id, dispatch_no), 
	FOREIGN KEY(step_id) REFERENCES task_steps (id)
);

CREATE TABLE task_shares (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	recipient_user_id VARCHAR(36) NOT NULL, 
	granted_by VARCHAR(36) NOT NULL, 
	granted_at DATETIME NOT NULL, 
	expires_at DATETIME, 
	revoked_at DATETIME, 
	status VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (task_id, recipient_user_id), 
	FOREIGN KEY(task_id) REFERENCES query_tasks (id), 
	FOREIGN KEY(recipient_user_id) REFERENCES users (id), 
	FOREIGN KEY(granted_by) REFERENCES users (id)
);

CREATE TABLE task_steps (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	step_key VARCHAR(100) NOT NULL, 
	position INTEGER NOT NULL, 
	kind VARCHAR(80) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	attempt_count INTEGER NOT NULL, 
	lease_until DATETIME, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (task_id, step_key), 
	FOREIGN KEY(task_id) REFERENCES query_tasks (id)
);

CREATE TABLE users (
	id VARCHAR(36) NOT NULL, 
	display_name VARCHAR(200) NOT NULL, 
	username VARCHAR(200) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	credential_hash TEXT, 
	credential_state VARCHAR(20) NOT NULL, 
	credential_revision INTEGER NOT NULL, 
	authz_revision INTEGER NOT NULL, 
	roles JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_api_idempotency_records_actor_user_id ON api_idempotency_records (actor_user_id);

CREATE INDEX ix_audit_events_action ON audit_events (action);

CREATE INDEX ix_audit_events_actor_user_id ON audit_events (actor_user_id);

CREATE UNIQUE INDEX ix_auth_sessions_token_hash ON auth_sessions (token_hash);

CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id);

CREATE INDEX ix_authorization_grants_document_id ON authorization_grants (document_id);

CREATE INDEX ix_authorization_grants_knowledge_base_id ON authorization_grants (knowledge_base_id);

CREATE INDEX ix_authorization_grants_operation ON authorization_grants (operation);

CREATE INDEX ix_authorization_grants_subject_user_id ON authorization_grants (subject_user_id);

CREATE INDEX ix_chunk_sets_parse_run_id ON chunk_sets (parse_run_id);

CREATE INDEX ix_content_blocks_parse_run_id ON content_blocks (parse_run_id);

CREATE INDEX ix_content_blocks_review_status ON content_blocks (review_status);

CREATE INDEX ix_document_versions_applicability_status ON document_versions (applicability_status);

CREATE INDEX ix_document_versions_content_review_status ON document_versions (content_review_status);

CREATE INDEX ix_document_versions_document_id ON document_versions (document_id);

CREATE INDEX ix_document_versions_publish_status ON document_versions (publish_status);

CREATE INDEX ix_documents_knowledge_base_id ON documents (knowledge_base_id);

CREATE INDEX ix_file_objects_sha256 ON file_objects (sha256);

CREATE INDEX ix_file_objects_status ON file_objects (status);

CREATE INDEX ix_index_generations_knowledge_base_id ON index_generations (knowledge_base_id);

CREATE INDEX ix_index_generations_status ON index_generations (status);

CREATE INDEX ix_index_sources_document_version_id ON index_sources (document_version_id);

CREATE INDEX ix_index_sources_index_generation_id ON index_sources (index_generation_id);

CREATE INDEX ix_knowledge_bases_status ON knowledge_bases (status);

CREATE INDEX ix_parse_runs_document_version_id ON parse_runs (document_version_id);

CREATE INDEX ix_parse_runs_status ON parse_runs (status);

CREATE INDEX ix_provider_calls_provider ON provider_calls (provider);

CREATE INDEX ix_query_tasks_owner_user_id ON query_tasks (owner_user_id);

CREATE INDEX ix_query_tasks_status ON query_tasks (status);

CREATE INDEX ix_task_artifacts_task_id ON task_artifacts (task_id);

CREATE INDEX ix_task_attempts_step_id ON task_attempts (step_id);

CREATE INDEX ix_task_evidence_task_id ON task_evidence (task_id);

CREATE INDEX ix_task_outbox_step_id ON task_outbox (step_id);

CREATE INDEX ix_task_shares_recipient_user_id ON task_shares (recipient_user_id);

CREATE INDEX ix_task_shares_task_id ON task_shares (task_id);

CREATE INDEX ix_task_steps_task_id ON task_steps (task_id);

CREATE INDEX ix_users_status ON users (status);

CREATE UNIQUE INDEX ix_users_username ON users (username);
