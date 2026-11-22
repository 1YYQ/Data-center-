import { useEffect, useState } from "react";
import { api } from "../../api";
import { createProjectionLease } from "../../projectionLease";
import { formatTime, type Approval } from "./types";

export type Grant = { id: string; approval_id: string; operation: string; effective: boolean; knowledge_base_id: string | null; document_id: string | null; level: string; category: string; need_class: string; valid_until: string | null };
const names: Record<string,string> = {"document.read":"查看原文", "knowledge.query":"查询资料", "knowledge.manage":"维护资料", "feedback.handle":"处理资料反馈"};
type Perform = (action: () => Promise<void>, message?: string) => Promise<void>;

export function ApprovalGrant({ row, perform }: {row: Approval; perform: Perform}) {
  const [grants, setGrants] = useState<Grant[] | null>(null), [message, setMessage] = useState("");
  const [operation, setOperation] = useState(row.operations[0] || "");
  async function read() {
    const rows: Grant[] = []; let offset: number | null = 0;
    while (offset !== null) {
      const result: {items: Grant[]; next_offset: number | null} = await api.get(`/admin/grants?user_id=${encodeURIComponent(row.applicant)}&limit=100&offset=${offset}`);
      rows.push(...result.items.filter(grant => grant.approval_id === row.id)); offset = result.next_offset;
    }
    return rows;
  }
  useEffect(() => {
    let active = true, pending = false;
    const lease = createProjectionLease(() => { if (active) setGrants(null); });
    async function refresh() {
      if (pending || document.hidden) return;
      pending = true; const started = performance.now();
      try {const rows = await read(); if (active && lease.renew(started)) {setGrants(rows); setMessage("");}}
      catch {if (active) {lease.invalidate(); setMessage("授权记录暂不可核对，请恢复连接后刷新。");}}
      finally {pending = false;}
    }
    void refresh(); const timer = setInterval(() => void refresh(),10000);
    return () => {active = false; lease.dispose(); clearInterval(timer);};
  }, [row.id]);
  const allowed = row.current === true && row.verification_state === "verified" && row.decision === "approved";
  const existing = grants?.some(grant => grant.operation === operation && grant.effective);
  return <section className="work-section" aria-label="按审批创建资料授权"><h3>落实资料授权</h3>
    <p className="muted">核查通过后，还需显式创建授权。用户、资料范围与截止时间固定采用本审批；角色不会自动授予资料权限。</p>
    <p>授权用户：{row.applicant} · 截止 {formatTime(row.valid_until)}</p>
    <ul>{grants?.map(grant => <li key={grant.id}>{names[grant.operation] || grant.operation} · {grant.effective ? "授权记录有效" : "当前不可用"} <small>{grant.id}</small></li>)}</ul>
    {grants === null && <p role="status">{message || "正在核对已有授权…"}</p>}
    <form className="review-form" onSubmit={event => {event.preventDefault(); void perform(async () => {
      if (!allowed || !grants || existing || !row.operations.includes(operation)) throw Error("请先核对当前审批与授权状态。");
      await api.mutate("/admin/grants", {...row.scope, user_id: row.applicant, approval_id: row.id, operation, valid_until: row.valid_until});
      setGrants(await read());
    }, "资料授权已创建；实际访问仍逐次复核当前审批、期限和资料状态。");}}>
      <label>本次允许操作<select value={operation} onChange={event => setOperation(event.target.value)}>{row.operations.map(value => <option key={value} value={value}>{names[value] || value}</option>)}</select></label>
      <button disabled={!allowed || !grants || !operation || existing}>{existing ? "此项已有有效授权" : "创建此项资料授权"}</button>
    </form>{!allowed && <p className="block-note">须先完成当前有效审批的核查；待核查、被拒绝、过期或已撤销记录不能创建授权。</p>}
  </section>;
}
