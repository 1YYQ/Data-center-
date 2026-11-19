# Phase 1 Data Model：首期设备操作维护查询

本模型只覆盖首期查询、资料治理、登录会话和权限隔离。后续计算、Excel/Word 导出和多资料报告不在本模型内。

## 核心实体

| 实体 | 关键字段 | 约束与说明 |
| --- | --- | --- |
| User | id, display_name, status, authz_revision | `active/disabled`；停用不删除历史任务归属 |
| UserIdentity | id, user_id, provider, subject, credential_hash?, credential_algorithm?, credential_updated_at, credential_state, credential_revision | 身份映射与业务用户分离；本地账号只保存经批准口令 KDF 的摘要，不保存明文口令或会话令牌 |
| AuthSession | id, user_id, token_hash, issued_at, expires_at, revoked_at, authz_revision | 只保存不可逆令牌摘要；停用、撤权或修订变化时失效 |
| Role | id, code, name | 初始角色为普通使用者、知识管理员、系统管理员 |
| RoleAssignment | user_id, role_id, valid_from, valid_until, revoked_at, granted_by | 角色生命周期和登记人；资料范围与操作由 AuthorizationGrant 保存 |
| Permission | id, code, description | 能力码由服务端种子维护 |
| AuthorizationGrant | id, subject_user_id/role_id, knowledge_base_id/document_id, operation, level/category/need_class, valid_from, valid_until, revoked_at, approval_id, granted_by | 一条授权保存完整匹配元组；不能跨授权记录拼接 |
| BusinessApproval | id, applicant, approver, approved_at, scope, operations, validity, evidence_locator, status | 业务审批与系统登记分开保存；缺审批依据不能启用授权 |
| KnowledgeBase | id, name, status, content_revision, acl_revision, active_generation_id | 活动索引属于该库且必须通过完整性校验 |
| FileObject | id, storage_name, object_key, sha256, mime_type, status | 原件和派生文件独立存储；下载走业务上下文授权 |
| Document | id, knowledge_base_id, title, category, access_mode, deleted_at | 文档归属固定；权限可继承或收紧 |
| DocumentVersion | id, document_id, revision_label, applicability, applicability_status, content_review_status, publish_status, source_file_id, effective_at | `applicability_status=pending/confirmed/conflicted/withdrawn`；`content_review_status=unreviewed/approved/blocked`；`publish_status=draft/effective/superseded/withdrawn` |
| ApprovalRecord | id, document_version_id, business_approval_id, registration_user_id, registered_at, reason | 保存确认、登记、依据和状态变更历史 |
| ParseRun | id, document_version_id, origin, provider_task_id, status, output_revision | 远端重试复用任务标识；人工修正创建新批次 |
| ContentBlock | id, parse_run_id, kind, text_content, structured_content, locator, review_status | `approved/rejected/unreviewed`；关键识别歧义进入待核验 |
| ChunkSet | id, parse_run_id, splitter_version, status | 只允许来自同一解析批次的块组成切分集合 |
| IndexGeneration | id, knowledge_base_id, embedding_profile_id, source_revision, status | ready 代际显式启用；不能混用向量配置 |
| QueryTask | id, owner_user_id, kind, input_snapshot, status, next_action | 输入冻结；重试是执行尝试，不创建新业务任务 |
| TaskEvidence | id, task_id, document_version_id, block_id, quote_snapshot, locator, review_at_run | 任务结果引用的每个证据都可回溯原文 |
| TaskShare | task_id, recipient_user_id, granted_by, granted_at, expires_at, revoked_at, status | 首期只映射单条回答的接收关系，保留设备条件/版本/出处；不分享对话，不授予资料 ACL；读取时重新检查依赖 |
| TaskArtifact | id, task_id, kind, validation_status, file_object_id | 首期主要保存查询结果 JSON/文本及引用映射 |
| AuditEvent | id, actor, action, resource, reason, created_at | 记录授权、版本、核验、发布、分享和撤权事件 |

## 关键关系

```text
User ──< AuthSession
User ──< RoleAssignment >── Role ──< RolePermission >── Permission
User/Role ──< AuthorizationGrant >── KnowledgeBase/Document
Document ──< DocumentVersion ──< ParseRun ──< ContentBlock
ParseRun ──< ChunkSet ──< IndexGeneration >── KnowledgeBase
User ──< QueryTask ──< TaskEvidence >── DocumentVersion
QueryTask ──< TaskShare
QueryTask ──< TaskArtifact
```

## 状态与不变量

1. 只有 active 用户、有效会话、匹配的能力和完整授权元组才能读取或操作资料。
2. 只有发布状态effective、业务适用性confirmed、内容核验approved且当前型号/权限/选用批次与索引就绪的版本才能进入正式查询。draft原件默认管理隔离；2026-11-17用户确认DM可显式将精确draft版本reference_access_state设为open，员工仍须当前DR，只作人工原文参考。未开放草稿不可见；参考开放不产生正式资格。字段与事务在数据库17.6、动作在API17.6维护；当前物理实现及有限验证见STATUS，不能由本逻辑模型推导真实验收通过。
3. 解析成功不等于内容已核验；索引 ready 不等于已启用；任务 succeeded 不等于业务人员已复核。
4. 证据、任务和产物保存历史快照，同时在读取时重新检查当前权限和版本状态。
5. 撤权、停用、过期或分享被撤销后阻止后续查询、预览、下载和分享读取；已下载到用户设备的内容无法追回，须在交付说明中明确。
6. 任何跨实体约束必须由数据库约束、事务检查和服务端授权共同保证，不能只依赖前端隐藏菜单。


## 对话与查阅扩展（2026-11-17）

FR-015/FR-016 的产品能力已确认，对话、消息与条件修订的候选结构集中见 [数据库 13](../../doc/database/数据库设计.md#13-对话消息与条件快照候选设计)，不在本文件再定义一套表字段。需要检索的回合关联既有 QueryTask，证据与分享复用 TaskEvidence/TaskShare；跨库手册目录复用现有文档/版本/原件，不复制业务文件。当前尚无新增迁移或应用验收。

## 资料问题反馈扩展（2026-11-17）

FR-017 确认当前对话/手册页提交及知识管理员后续处理，逻辑结构集中见 [数据库 13.3](../../doc/database/数据库设计.md#133-资料问题反馈候选设计)。反馈来源和资料依赖独立保存，不替代业务审批、内容核验、版本发布或回答分享；处理状态及分派规则仍为候选，尚无迁移或应用验收。

## PDF Processing 实施映射（2026-11-18）

字段/类型/约束的唯一权威是[数据库22](../../doc/database/数据库设计.md#22-document-ir-数据契约候选01)，本节只给增量实现落点，避免第二套字段定义。计划不等于新增ORM或迁移；上述2026-11-17“尚无实现”是形成时记录，当前实现资格从STATUS及代码核对。

| 数据责任 | 复用／新增及工程落点 |
| --- | --- |
| 原PDF/业务版本 | 复用Document/DocumentVersion/FileObject；业务版本与Run、ChunkSet、代际四种身份不混用 |
| Run原始证据 | 扩ParseRun/ParseServiceJob；实现parse_artifacts关联私有FileObject，attempt原输出及父来源保留；P1 |
| 归一化 | document_pages、ContentBlock来源/几何、Asset视图、Normalized artifact和同manifest DB投影；P2 |
| 结构和核验 | ContentBlock容器＋block_relations；Structured artifact、源QC报告、独立BusinessConfirmation/VerificationEvent/hold证明；P3 |
| 切分与候选 | chunks/chunk_blocks及ChunkSet真实配置/成员/一层parent；集合QC、精确候选ReviewSet与Publication/source_rebind资格；P4 |
| 检索与证据 | immutable embedding_profiles、generation范围chunk_embeddings、expected manifest及活动指针；TaskEvidence保留Chunk/Block/region/原范围及历史来源；P5 |

里程碑唯一按[SPEC10.4.1](../../doc/requirements/SPEC.md#1041-处理技术质量与人工审核状态)派生：Parsed看Run级完整页来源，Normalized/Structured各有冻结产物，QC Passed分源/集合，Verified分源/候选，Published另需业务适用依据与显式事务。Run.succeeded不等于候选Verified；候选ReviewSet不改变selected；审核证明不回写冻结IR或关系。

迁移分P1产物、P2页/来源、P3关系、P4真实Chunk、P5代际成员，均沿现有安全迁移入口。原Block向量和旧引用保留真实身份，legacy_incomplete不补造坐标/原包或资格；不可用旧代际不能以回滚复活。复合FK、Unicode区间、资源引用、关系投影和逐层一致性验证分别进入相应阶段任务。
