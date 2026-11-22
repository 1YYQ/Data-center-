import { useEffect, useState } from "react";
import { api } from "../../api";
import { Workspace, Status, useOperation } from "../../components";
import { stateLabel } from "../../labels";
import { useSession } from "../../session";
import { createProjectionLease } from "../../projectionLease";

type ServiceProfile = { id: string; label: string; enabled: boolean; active_revision_id: string | null; current_test_ids: string[] };
type Selection = { purpose: string; enabled: boolean; profile_id: string; revision_id: string; test_run_id: string };
const purposes = [["query.answer", "查询答复", "根据授权证据组织答复"], ["document.embed", "资料检索", "将已核验资料用于检索"], ["document.parse", "文档解析", "处理收录的 PDF 原件"]];

type OperationTask = { id: string; status: string; error_code: string | null; created_at: string };
export function Operations() {
  return <Workspace title="运行与异常" eyebrow="" intro="查看服务准入与近期任务，定位需要处理的异常。" role="system_admin"><OperationsContent/></Workspace>;
}
function OperationsContent() {
  const op = useOperation(), { profile } = useSession();
  const [data, setData] = useState<{tasks: OperationTask[]; profiles: ServiceProfile[]; selections: Selection[]} | null>(null);
  const [observed, setObserved] = useState(""), [error, setError] = useState("");
  const canConfigure = profile!.capabilities.includes("system.configure");
  useEffect(() => {
    let active = true, pending = false;
    const lease = createProjectionLease(() => { if (active) setData(null); });
    async function refresh() {
      if (pending || document.hidden) return;
      pending = true; const started = performance.now();
      try {
        const [tasks, profiles, selections] = await Promise.all([
          api.get<{items: OperationTask[]}>("/admin/operations/tasks"),
          canConfigure ? api.get<{items: ServiceProfile[]}>("/admin/service-profiles") : Promise.resolve({items: []}),
          canConfigure ? api.get<{items: Selection[]}>("/admin/service-selections") : Promise.resolve({items: []}),
        ]);
        if (active && lease.renew(started)) { setData({tasks: tasks.items, profiles: profiles.items, selections: selections.items}); setError(""); setObserved(new Date().toLocaleTimeString("zh-CN", {hour12:false})); }
      } catch { if (active) { lease.invalidate(); setError("运行状态暂不可核对，请恢复连接后刷新。"); } }
      finally {pending = false;}
    }
    void refresh(); const timer = setInterval(() => void refresh(), 10000);
    window.addEventListener("focus", refresh);
    return () => {active = false; clearInterval(timer); lease.dispose(); window.removeEventListener("focus", refresh);};
  }, [canConfigure]);
  const tasks = data?.tasks || [], exceptions = tasks.filter(row => ["failed", "blocked", "reconciliation_required"].includes(row.status));
  return <div className="operations-dashboard"><div className="operations-toolbar"><span>{data ? `最近核对 ${observed}` : "状态待核对"}</span><button className="ghost" disabled={op.busy} onClick={() => location.reload()}>刷新运行状态</button></div>
    {canConfigure && <section className="admission-overview" aria-label="用途准入状态"><div className="section-heading"><div><h2>服务准入</h2><p className="muted">分别核对各用途的选用与有效自检。</p></div><a href="/workspace/services">管理配置 →</a></div><div className="admission-grid">{purposes.map(([purpose,title,description]) => {
      const selection = data?.selections.find(row => row.purpose === purpose), configured = data?.profiles.find(row => row.id === selection?.profile_id);
      const valid = selection?.enabled && configured?.enabled && configured.active_revision_id === selection.revision_id && configured.current_test_ids.includes(selection.test_run_id);
      return <article className="admission-tile" key={purpose}><span className={`record-state ${data && valid ? "ready" : "needs-action"}`}>{!data ? "待核对" : !selection?.revision_id ? "未配置用途" : valid ? "准入有效" : "需要处理"}</span><h3>{title}</h3><p>{description}</p><footer>{configured?.label || "尚未选用服务"}</footer></article>;
    })}</div><p className="locator">当前仅获准合成试点。准入有效表示配置具备使用资格，实际请求仍可能遇到异常；此页不发起模型调用。</p></section>}
    <div className="operations-body"><section className="panel operation-register"><div className="section-heading"><div><h2>近期任务</h2><p className="muted">已载入 {tasks.length} 项 · {exceptions.length} 项需核对。仅展示脱敏状态。</p></div></div><div className="table-scroll"><table><thead><tr><th>时间 / 任务</th><th>当前状态</th><th>处理提示</th></tr></thead><tbody>{tasks.map(row => <tr key={row.id}><td><span>{new Date(row.created_at).toLocaleString("zh-CN")}</span><details><summary>任务编号</summary><code>{row.id}</code></details></td><td><span className={`record-state ${["failed","blocked","reconciliation_required"].includes(row.status) ? "needs-action" : ""}`}>{stateLabel(row.status)}</span></td><td>{row.status === "reconciliation_required" ? "先核对原调用回执，勿重复派发。" : row.error_code ? "核对服务准入及原回执；由任务所有者接续。" : ["running","pending"].includes(row.status) ? "保留原队列，等待消费者处理。" : "当前无额外处理提示。"}</td></tr>)}</tbody></table></div>{!tasks.length && <p className="empty">{data ? "当前没有可列出的任务记录。" : "正在核对运行状态；未确认前不显示旧记录。"}</p>}</section>
    <aside className="operations-guide"><h2>处理异常</h2><ol><li><strong>检查用途准入</strong><p>核对当前选用、停用状态与自检有效期。</p></li><li><strong>核对原任务</strong><p>结果未知时先查回执，保留原任务和预算。</p></li><li><strong>由原用户接续</strong><p>恢复服务后，由任务所有者重试可重试的查询。</p></li></ol><a href="/workspace/accounts">账号与授权 →</a><a href="/workspace/access-review">审批依据 →</a><p className="locator">本地备份保持停用。此页不会执行备份，也不读取业务正文。</p></aside></div><Status message={error} bad={!!error}/></div>;
}
