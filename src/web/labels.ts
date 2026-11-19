const labels: Record<string, string> = {
  RETENTION_POLICY_UNCONFIGURED: "保留策略尚未配置", PERMANENT_PURGE_NOT_AVAILABLE: "当前不提供永久清理", missing: "缺失",
  draft: "草稿", effective: "当前发布版", superseded: "历史发布版", withdrawn: "已撤回",
  pending: "待处理", running: "处理中", succeeded: "处理成功", needs_review: "待核验", failed: "处理失败", blocked: "受阻", ready: "已就绪", stale: "已过期，须重建", active: "已启用", not_ready: "尚未就绪", empty: "尚无可用索引", building: "构建中",
  requires_rebuild: "须重建索引", requires_rebuild_after_publication: "发布后须重建索引",
  applicable: "适用", not_applicable: "不适用", accepted: "关键内容已确认", rejected: "不接受", classified: "分类已确认", conflict: "存在冲突",
  SOURCE_UNAVAILABLE: "原始文件当前不可用", ACCESS_RULES_INCOMPLETE: "资料访问分类尚不完整", MODEL_SCOPE_MISSING: "缺少确认适用的设备型号", SOURCE_METADATA_INCOMPLETE: "资料来源信息不完整", BUSINESS_CONFIRMATION_PENDING: "业务适用性确认尚未核实或存在冲突", CLASSIFICATION_CONFIRMATION_PENDING: "资料分类确认尚未核实或存在冲突", REVIEW_COVERAGE_INCOMPLETE: "完整核验集缺失、覆盖不足或已失效", DOCUMENT_USE_HELD: "此版本已被暂停使用", VERSION_NOT_DRAFT: "此版本不是可发布的草稿", VERSION_NOT_PUBLISHED: "此版本尚未发布，不能撤回"
};
export const stateLabel = (value: string) => labels[value] || value;
