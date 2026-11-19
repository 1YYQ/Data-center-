import Modal from "antd/es/modal";
import { useRef } from "react";
import { api } from "../../api";
import { Field, formValues, Status, text } from "../../components";
import type { Approval } from "./types";

export function ApprovalRegistration({ open, onClose, approvals, busy, message, bad, perform, onRegistered }: { open: boolean; onClose: () => void; approvals: Approval[]; busy: boolean; message: string; bad: boolean; perform: (action: () => Promise<void>, success?: string) => Promise<void>; onRegistered: (id: string) => Promise<void> }) {
  const formRef = useRef<HTMLFormElement>(null);
  return <Modal open={open} onCancel={() => { if (!busy) onClose(); }} closable={!busy} title="登记真实审批事实" footer={null} width={820} maskClosable={false} destroyOnHidden><Status message={message} bad={bad} /><form ref={formRef} onSubmit={event => { event.preventDefault(); const values = formValues(event); void perform(async () => {
    const from = new Date(text(values, "valid_from")), until = new Date(text(values, "valid_until")), approved = new Date(text(values, "approved_at"));
    if (![from, until, approved].every(value => Number.isFinite(value.valueOf())) || until <= from || approved > new Date()) throw Error("请核对实际审批时间及有效期限。");
    const result = await api.mutate<Approval>("/admin/business-approvals", {
      applicant: text(values, "applicant"), decision: text(values, "decision"), approved_at: approved.toISOString(), valid_from: from.toISOString(), valid_until: until.toISOString(),
      actual_approver: { identity_label: text(values, "identity_label"), responsibility: text(values, "responsibility"), ...(text(values, "approver_user_ref") ? { user_ref: text(values, "approver_user_ref") } : {}), authority_basis_ref: text(values, "authority_basis_ref") },
      scope: { knowledge_base_id: text(values, "knowledge_base_id"), document_id: text(values, "document_id") || null, level: text(values, "level"), category: text(values, "category"), need_class: text(values, "need_class") },
      operations: values.getAll("operations"), evidence_locator: text(values, "evidence_locator"), proof_refs: [], supersedes_id: text(values, "supersedes_id") || null
    }); onClose(); await onRegistered(result.id);
  }, "事实已登记为待核查；登记操作没有批准授权。"); }}><fieldset className="operation-fields" disabled={busy}><p className="warning">填写系统外实际发生的批准或拒绝。登记操作本身只创建待核查记录，不会批准授权。</p><div className="review-form register-grid">
    <Field label="被授权用户 ID" name="applicant" /><label>外部决定<select name="decision" required><option value="approved">实际批准</option><option value="rejected">实际拒绝</option></select></label><Field label="实际审批人" name="identity_label" /><Field label="审批职责" name="responsibility" /><Field label="审批人平台账号引用（如适用）" name="approver_user_ref" required={false} /><Field label="审批时间" name="approved_at" type="datetime-local" /><Field label="授权开始" name="valid_from" type="datetime-local" /><Field label="授权截止" name="valid_until" type="datetime-local" /><Field label="知识库 ID" name="knowledge_base_id" /><Field label="文档 ID（可选）" name="document_id" required={false} /><Field label="资料等级" name="level" /><Field label="资料类别" name="category" /><Field label="用途分类" name="need_class" /><label className="wide">允许操作<select name="operations" multiple required><option value="document.read">查看文档</option><option value="knowledge.query">知识查询</option><option value="feedback.handle">受理资料反馈</option></select><span className="field-hint">按住 Ctrl / ⌘ 可选择多项。</span></label><Field label="实际审批人的职责依据编号" name="authority_basis_ref" /><Field label="外部审批来源说明" name="evidence_locator" /><label className="wide">替代已有登记（可选）<select name="supersedes_id"><option value="">不替代</option>{approvals.map(row => <option key={row.id} value={row.id}>{row.applicant} · {row.id}</option>)}</select></label></div><div className="dialog-actions"><button className="ghost" type="button" onClick={onClose}>取消</button><button>保存为待核查</button></div></fieldset></form></Modal>;
}
