export type Evidence = { id: string; source_description: string; attested: boolean; linked: boolean };
export type Authority = { id: string; identity_label: string };
export type Approval = {
  id: string; revision: number; etag: string; applicant: string; decision: string; status: string;
  verification_state: string; current: boolean; revoked: boolean; revocation_id?: string;
  approved_at: string | null; recorded_by: string; valid_from: string | null; valid_until: string | null;
  actual_approver: { identity_label: string; responsibility: string }; scope: { knowledge_base_id: string; document_id: string | null; level: string; category: string; need_class: string };
  operations: string[]; evidence_locator: string; verified_by: string; verified_at: string | null; proof_refs: string[];
};
export type VerificationOptions = { authority_bases: Authority[]; evidence: Evidence[]; proof_refs: string[]; revision: number };
export function recordStatus(row: Approval) {
  if (row.revoked) return ["已撤销", "state-revoked"];
  if (row.current === false && row.verification_state === "verified") return ["当前不可用", "state-revoked"];
  if (row.verification_state === "verified") return ["已核查", "state-verified"];
  if (row.verification_state === "rejected") return ["未通过", "state-rejected"];
  return ["待核查", "state-unverified"];
}
export const formatTime = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN") : "未登记";
export function verificationBlocked(row: Approval, options: VerificationOptions) {
  const reasons: string[] = [], scope = row.scope;
  if (row.decision !== "approved") reasons.push("外部决定不是批准");
  if (row.verification_state === "verified" || row.status === "approved") reasons.push("本记录已核查，变更依据请追加替代登记");
  if (!row.approved_at || !row.valid_from || !row.valid_until || new Date(row.valid_until) <= new Date(row.valid_from)) reasons.push("批准时间或完整有效期限缺失");
  if (!(scope.knowledge_base_id || scope.document_id) || !scope.level || !scope.category || !scope.need_class || !row.operations.length) reasons.push("授权范围、资料分类或允许操作不完整");
  const good = new Set(options.evidence.filter(item => item.attested && item.linked).map(item => item.id));
  if (!options.proof_refs.length || options.proof_refs.some(ref => !good.has(ref))) reasons.push("证据缺失、未关联或未受控，普通上传材料不能支持通过");
  if (!options.authority_bases.length) reasons.push("没有服务端确认的当前有效审批职责依据");
  return reasons;
}
