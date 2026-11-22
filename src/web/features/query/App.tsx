import { useEffect, useRef, useState } from "react";
import { api, errorMessage, HttpError, id, StaleSessionError, type PendingCommand } from "../../api";
import { AnswerSharing } from "./AnswerSharing";
import { formValues, Status, text, useOperation, Workspace } from "../../components";
import { useSession } from "../../session";
import { FeedbackPanel } from "../feedback";
import { EvidenceCard, type Evidence } from "./EvidenceCard";
import { Manuals } from "./Manuals";
import { createProjectionLease } from "../../projectionLease";

type QueryInput = { query: string; model: string | null; mode: "retrieve" | "answer"; conversation_id?: string; context_revision?: number };
type Task = { id: string; kind?: string; status: string; input?: QueryInput | null; effective_input?: QueryInput | null; next_action?: string; conversation_id?: string; retry_eligible?: boolean; revision?: number; created_at?: string; error_code?: string; result?: { model?: string; result_status?: string } };
type InputRequest = { id: string; revision: number; status: string; reason: string };
type ResultCoverage = { missing_count?: number; missing_item_refs?: string[]; items?: { label?: string; state: string }[] };
type Result = { answer: string | null; result_status: string; warnings: string[]; evidence: Evidence[]; answer_snapshot_id?: string | null; sections?: { heading: string; claim_ids: string[] }[]; claims?: { claim_id: string; text: string }[]; coverage?: ResultCoverage | null; task?: { id?: string; input?: QueryInput | null; result?: { model?: string; coverage?: ResultCoverage | null } } };
const warnings: Record<string, string> = { version_conflict: "存在版本冲突，须由负责人确认适用版本", index_stale: "资料更新后仍在准备查询", index_not_ready: "资料尚未准备好查询", partial_index: "部分资料尚未准备完成", evidence_pending_review: "原文内容仍待核验", content_pending_review: "手册仍待核验", publication_not_ready: "手册尚未满足正式使用条件", insufficient_evidence: "缺少足够依据", access_limited: "资料访问权限不足", model_or_evidence_mismatch: "型号或适用资料尚不明确" };
const resultBadges: Record<string, string> = { formal: "已完成", extractive: "原文摘录", partial: "依据部分覆盖", needs_review: "待人工核对" };
const resultGuidance: Record<string, string> = { extractive: "以下是原文摘录，尚非正式组织答案。请结合上下文核对。", partial: "已找到部分依据，其余内容仍需核对。", needs_review: "现有依据仍需复核，暂未生成可使用的答复。" };
const renderableResults = new Set(["formal", "extractive", "partial"]);
const statusNames: Record<string, string> = { pending: "排队中", running: "执行中", waiting_input: "等待补充", succeeded: "结果可查看", failed: "执行未完成", blocked: "需要处理", cancelled: "已取消", access_limited: "访问条件已变化" };

export function QueryApp() {
  const legacyVersion = new URLSearchParams(location.search).get("manual");
  if (legacyVersion) return <Manuals legacyVersionId={legacyVersion}/>;
  return <Workspace title="查询设备资料" eyebrow="" intro="查操作步骤、维护要求和注意事项，答复附手册版本与原文出处。"><QueryWorkspace /></Workspace>;
}
function QueryWorkspace() {
  const { profile } = useSession(), owner = profile!.user.id, op = useOperation();
  const [query, setQuery] = useState(""), [model, setModel] = useState(""), [task, setTask] = useState<Task | null>(null), [result, setResult] = useState<Result | null>(null), [history, setHistory] = useState<Task[]>([]), [request, setRequest] = useState<InputRequest | null>(null), [status, setStatus] = useState(""), [badge, setBadge] = useState(""), [historyError, setHistoryError] = useState(""), [historyNext, setHistoryNext] = useState<number | null>(null), [historyBusy, setHistoryBusy] = useState(false);
  const [condition, setCondition] = useState<{ conversation_id: string; revision: number; latest_task_id: string | null } | null>(null);
  const [executions, setExecutions] = useState<Task[]>([]), [executionsNext, setExecutionsNext] = useState<number | null>(null);
  const historyOffset = useRef(0);
  const historicalView = new URLSearchParams(location.search).has("history");
  const active = useRef(true), timer = useRef<ReturnType<typeof setTimeout> | null>(null), seq = useRef(0), chosen = useRef<string | null>(null), historySeq = useRef(0);
  const polling = useRef<{ task: string; token: number } | null>(null);
  const pendingMetadata = useRef<Task | null>(null);
  const resultLease = useRef<ReturnType<typeof createProjectionLease> | null>(null), historyLease = useRef<ReturnType<typeof createProjectionLease> | null>(null);
  const executionsLease = useRef<ReturnType<typeof createProjectionLease> | null>(null);
  resultLease.current ||= createProjectionLease(() => { setResult(null); setStatus("结果的访问资格待重新核对，已隐藏内容。"); });
  historyLease.current ||= createProjectionLease(() => setHistory([]));
  executionsLease.current ||= createProjectionLease(() => setExecutions([]));
  const commands = api.pendingCommands(), unknown = commands.find(command => command.path === "/knowledge/queries" || /^\/tasks\/[^/]+\/(retry|cancel|input-requests\/[^/]+\/responses)$/.test(command.path));
  const storage = `dc-workbench:${owner}`;
  const groups = history;
  const example = groups.find(row => row.input?.query === "SYNTHETIC-1 startup and pressure");
  function persist(taskId: string) { chosen.current = taskId; if (!historicalView) sessionStorage.setItem(storage, JSON.stringify({ task: taskId })); window.history.replaceState(null, "", "/workspace/query?task=" + id(taskId) + (historicalView ? "&history=1" : "")); }
  function clearTimer() { if (timer.current) clearTimeout(timer.current); }
  function fail(error: unknown) { if (!active.current || error instanceof StaleSessionError) return; setResult(null); setRequest(null); setBadge("工作未完成"); setStatus(errorMessage(error)); }
  async function loadHistory(more = false, reset = false) {
    const started = performance.now(), token = ++historySeq.current, offset = more ? historyNext : reset ? 0 : historyOffset.current; if (offset === null) return;
    setHistoryBusy(true); setHistoryError("");
    try { const data = await api.get<{ items: Task[]; next_offset: number | null }>(`/workspace-data/works?limit=10&offset=${offset}`); if (!active.current || token !== historySeq.current || !historyLease.current!.renew(started)) return; setHistory(data.items); historyOffset.current = offset; setHistoryNext(data.next_offset); }
    catch (error) { if (active.current && token === historySeq.current) { setHistory([]); setHistoryError(errorMessage(error)); } }
    finally { if (active.current && token === historySeq.current) setHistoryBusy(false); }
  }
  async function poll() {
    const taskId = chosen.current; if (!taskId || polling.current?.task === taskId) return;
    clearTimer(); const token = ++seq.current; polling.current = { task: taskId, token };
    const current = () => active.current && chosen.current === taskId && token === seq.current;
    try {
    const progress = await api.get<Task>(`/tasks/${id(taskId)}/status`); if (!current()) return;
    if (progress.status === "access_limited") { setTask({ id: taskId, status: "access_limited" }); setResult(null); setRequest(null); setCondition(null); historySeq.current++; setHistory([]); setQuery(""); setModel(""); setBadge("访问条件已变化"); setStatus("当前工作不可访问，请核对权限或联系管理员。"); return; }
    if (["pending", "running"].includes(progress.status)) {
      // The lightweight status projection omits owner input. Restore the
      // separately authorized task once so refresh retains title/cancellation.
      if (pendingMetadata.current?.id !== taskId) {
        const restored = await api.get<Task>(`/tasks/${id(taskId)}`);
        if (!current()) return;
        pendingMetadata.current = restored;
      }
      setResult(null); setRequest(null); setTask({ ...pendingMetadata.current, ...progress });
      setBadge(statusNames[progress.status]); setStatus(progress.status === "pending" ? "工作已保存，等待执行。刷新不会重复提交。" : "正在核对资料与组织结果，工作已保存。");
      timer.current = setTimeout(() => void poll().catch(() => {}), 1000); return;
    }
    const latest = await api.get<Task>(`/tasks/${id(taskId)}`); if (!current()) return; setTask(latest); setBadge(latest.status === "succeeded" ? "正在核对结果" : statusNames[latest.status] || "工作未完成"); setStatus(latest.next_action || "");
    setHistory(rows => rows.map(row => row.id === latest.id ? latest : row));
    if (latest.conversation_id && latest.input) { const conditions = await api.get<{ conversation_id: string; revision: number; latest_task_id: string | null }>(`/conversations/${id(latest.conversation_id)}/conditions`); if (!current()) return; setCondition(conditions); } else setCondition(null);
    if (latest.status === "waiting_input") { const data = await api.get<{ items: InputRequest[] }>(`/tasks/${id(taskId)}/input-requests`); if (current()) { const input = data.items.find(item => item.status === "open") || null; setRequest(input); setStatus(input?.reason || "需要补充设备条件"); } }
    else setRequest(null);
    if (latest.status === "succeeded") { const started = performance.now(); const data = await api.get<Result>(`/tasks/${id(taskId)}/result`); if (current() && resultLease.current!.renew(started)) { setResult(data); setBadge(resultBadges[data.result_status] || "需要核对"); setStatus("结果已保存，请核对适用条件、警告和来源。"); } } else setResult(null);
    } catch (error) { if (current()) { fail(error); throw error; } }
    finally { if (polling.current?.token === token) polling.current = null; }
  }
  async function loadExecutions(more = false) {
    if (!task?.conversation_id) return;
    const started = performance.now(), selected = task.id;
    const data = await api.get<{ items: Task[]; next_offset: number | null }>(`/conversations/${id(task.conversation_id)}/executions?limit=10&offset=${more ? executionsNext || 0 : 0}`);
    if (!active.current || chosen.current !== selected || !executionsLease.current!.renew(started)) return;
    setExecutions(data.items); setExecutionsNext(data.next_offset);
  }
  async function openTask(row: Task) { if (unknown) throw Error("请先核对原操作回执。"); setResult(null); setRequest(null); setCondition(null); setExecutions([]); setTask(row); setQuery(""); setModel(""); persist(row.id); await poll(); }
  async function recover(command: PendingCommand) { const receipt = await api.inspectCommand(command); if (receipt.resource_refs.task_id) persist(receipt.resource_refs.task_id); await poll(); await loadHistory(); }
  async function submit(event: React.FormEvent) {
    event.preventDefault(); void op.perform(async () => {
      if (unknown) throw Error("原操作结果待核对，不能重复提交。");
      if (chosen.current && (!task?.input || !condition || historicalView || condition.latest_task_id !== chosen.current || task.status === "reconciliation_required")) throw Error("请先读取最新工作的状态，历史或待核对执行不能继续提交。");
      if (condition && task?.input) { const latest = await api.get<{ latest_task_id: string; revision: number }>(`/conversations/${id(condition.conversation_id)}/conditions`); if (latest.latest_task_id !== chosen.current || latest.revision !== condition.revision) throw Error("工作条件已有新修订，请刷新原工作再继续。"); }
      const input: QueryInput = { query: query.trim(), model: model.trim() || null, mode: "answer", ...(condition && task?.input ? { conversation_id: condition.conversation_id, context_revision: condition.revision } : {}) };
      if (!input.query) throw Error("请描述要完成的工作。");
      try { const data = await api.mutate<{ task_id: string }>("/knowledge/queries", input); persist(data.task_id); setResult(null); setRequest(null); setCondition(null); setTask({ id: data.task_id, status: "pending", input }); setQuery(""); await poll(); await loadHistory(); }
      catch (error) { if (error instanceof HttpError && error.status === 429) { setStatus("当前查询较多，本次执行未受理。已保存的工作保持不变，请稍后再提交。"); setBadge("执行未受理"); } else fail(error); throw error; }
    });
  }
  useEffect(() => {
    active.current = true; sessionStorage.removeItem("dc-workspace-query");
    const params = new URLSearchParams(location.search); let saved: { task?: string } = {};
    try { saved = JSON.parse(sessionStorage.getItem(storage) || "{}"); } catch { sessionStorage.removeItem(storage); }
    if (params.has("new")) { sessionStorage.removeItem(storage); window.history.replaceState(null, "", "/workspace/query"); }
    else { chosen.current = params.get("task") || saved.task || null; if (chosen.current) void poll().catch(() => {}); }
    void loadHistory();
    const offline = () => { seq.current++; historySeq.current++; clearTimer(); setResult(null); setRequest(null); setHistory([]); setTask(chosen.current ? { id: chosen.current, status: "access_limited" } : null); setCondition(null); setStatus("网络不可用，已隐藏受限内容。连接恢复后请核对原工作。"); };
    const focus = () => { if (document.hidden || !navigator.onLine) return; if (chosen.current) void poll().catch(() => {}); void loadHistory(); };
    const interval = setInterval(() => { if (!document.hidden && !op.busy) focus(); }, 10000);
    window.addEventListener("offline", offline); window.addEventListener("focus", focus); document.addEventListener("visibilitychange", focus); window.addEventListener("pageshow", focus);
    return () => { active.current = false; seq.current++; historySeq.current++; resultLease.current?.dispose(); historyLease.current?.dispose(); executionsLease.current?.dispose(); clearTimer(); clearInterval(interval); window.removeEventListener("offline", offline); window.removeEventListener("focus", focus); document.removeEventListener("visibilitychange", focus); window.removeEventListener("pageshow", focus); };
  }, [owner]);
  const readonly = Boolean(task && !task.input && !["pending", "running", "access_limited"].includes(task.status));
  const oldExecution = historicalView || Boolean(condition && condition.latest_task_id !== chosen.current);
  const title = workTitle(task?.input?.query) || (readonly ? "收到的单条回答" : "当前工作");
  return <div className={`task-workbench ${chosen.current ? "has-task" : ""}`}>
    {!chosen.current && status && <section className="warning" role="alert"><strong>{badge || "工作尚未受理"}</strong><p>{status}</p></section>}
    {!chosen.current && groups.some(row => row.status === "waiting_input") && <section className="attention-row"><div><h2>需要你补充</h2>{groups.filter(row => row.status === "waiting_input").map(row => <p key={row.id}>{row.input?.query || "已保存的工作"} <button className="ghost" disabled={op.busy || !!unknown} onClick={() => void op.perform(() => openTask(row))}>补充条件 →</button></p>)}</div></section>}
    {chosen.current && <section className="work-detail-heading"><a href="/workspace/query?new=1">← 返回工作台 / 新建工作</a><div className="section-heading"><h2>{title}</h2><span className="badge" role="status">{badge}</span></div>{(result?.task?.result?.model || task?.effective_input?.model) && <p className="muted">本次采用的设备型号 {result?.task?.result?.model || task?.effective_input?.model}</p>}<p role="status">{status}</p></section>}
    {unknown && <aside className="warning"><strong>原操作结果待核对</strong><p>只读取原回执，不重复执行；核实前不能创建新执行。</p><button disabled={op.busy} type="button" onClick={() => void op.perform(() => recover(unknown))}>恢复原提交</button></aside>}
    {result && <section className="panel result-panel" aria-label="工作结果"><QueryResult result={result} taskId={chosen.current!} hasFeedback={profile!.capabilities.includes("feedback.read_own") && !!task?.conversation_id} />{result.answer_snapshot_id && task?.input && profile!.capabilities.includes("task.share") && <AnswerSharing key={result.answer_snapshot_id} taskId={chosen.current!} snapshotId={result.answer_snapshot_id}/>}</section>}
    {chosen.current && <div className="work-actions"><button className="ghost" disabled={op.busy} onClick={() => void op.perform(poll)}>刷新原查询状态</button>{task?.retry_eligible && !unknown && !oldExecution && <button disabled={op.busy} onClick={() => void op.perform(async () => { const latest = await api.request<Task>(`/tasks/${id(chosen.current!)}`); await api.mutate(`/tasks/${id(chosen.current!)}/retry`, { reason: "服务恢复后重试原查询" }, latest.etag || undefined); await poll(); })}>重试原查询</button>}{task?.input && !oldExecution && ["pending", "running", "waiting_input"].includes(task.status) && !unknown && <button className="ghost" disabled={op.busy} onClick={() => void op.perform(async () => { const latest = await api.request<Task>(`/tasks/${id(chosen.current!)}`); await api.mutate(`/tasks/${id(chosen.current!)}/cancel`, { reason: "本人取消当前执行" }, latest.etag || undefined); await poll(); await loadHistory(); })}>取消本次执行</button>}</div>}
    {request && !oldExecution && <section className="attention-row"><div><h2>需要你补充</h2><p>{request.reason}</p></div><form className="input-form" onSubmit={event => { event.preventDefault(); const values = formValues(event); void op.perform(async () => { if (unknown) throw Error("请先核对原回执。"); await api.mutate(`/tasks/${id(chosen.current!)}/input-requests/${id(request.id)}/responses`, { model: text(values, "model") }, `"${request.revision}"`); setRequest(null); await poll(); await loadHistory(); }); }}><label>补充设备型号<input name="model" required maxLength={200}/></label><button disabled={op.busy || !!unknown}>补充并继续原任务</button></form></section>}
    {!chosen.current && <div className="query-start-heading"><span>操作步骤</span><span>维护要求</span><span>关键警告</span><span>原文查证</span></div>}
    {!readonly && !oldExecution && profile!.capabilities.includes("knowledge.query") && <section className="panel work-composer"><h2>{chosen.current ? "继续提问" : "你想了解什么？"}</h2><form className="stack" onSubmit={event => void submit(event)}><label>问题<textarea id="work-query" aria-label="问题" value={query} onChange={event => setQuery(event.target.value)} rows={4} required maxLength={5000} placeholder={chosen.current ? "继续追问，或补充设备条件…" : "例如：这台设备开机前需要检查什么？维护时有哪些注意事项？"}/></label><div className="composer-foot"><label>设备型号（可选）<input value={model} onChange={event => setModel(event.target.value)} maxLength={200} placeholder={condition ? "留空沿用当前已确认条件" : "有明确型号时填写"}/></label><button disabled={op.busy || !!unknown || !!request || (!!chosen.current && (!task?.input || !condition || condition.latest_task_id !== chosen.current)) || ["pending", "running", "access_limited", "reconciliation_required"].includes(task?.status || "")}>{chosen.current ? "继续查询" : "查询资料"}</button></div></form><p className="locator">{condition ? "追问沿用当前设备条件；可在上方填写新的型号。" : "不知道完整型号也可以先提问，缺少必要信息时会请你补充。"}</p></section>}
    {!chosen.current && <aside className="query-companion"><h2>拿到答案后，怎么核对？</h2><ol><li><strong>先看适用条件</strong><span>确认型号、版本与你的设备一致。</span></li><li><strong>留意警告与缺项</strong><span>没有依据的内容会说明，不替你猜测。</span></li><li><strong>打开引用原文</strong><span>从答复直接回到手册对应页。</span></li></ol><a href="/workspace/manuals">先浏览手册目录</a>{example?.input && <div className="pilot-example"><strong>当前为模拟试点</strong><p>当前试点仅开放固定示例的生成问答；也可浏览手册和查看已有答复。示例手册与答复为英文。</p><button className="ghost" onClick={() => { setQuery(example.input!.query); setModel(example.input!.model || ""); document.getElementById("work-query")?.focus(); }}>填入试点示例</button></div>}</aside>}
    {!chosen.current && !profile!.capabilities.includes("knowledge.query") && <p role="alert">当前账号没有资料查询权限，可切换至已获准的管理工作区。</p>}
    {oldExecution && <p className="warning">正在只读查看历史执行。<a href="/workspace/query">返回当前工作</a>后继续追问，旧答案及分享保持原身份。</p>}<Status message={op.message} bad={op.bad}/>
    {task?.input && task.conversation_id && <details onToggle={event => { if (event.currentTarget.open) void op.perform(() => loadExecutions()); else setExecutions([]); }}><summary>本项工作的历史执行与结果</summary>{executions.filter(row => row.id !== task.id).map(row => <p key={row.id}><a href={`/workspace/query?task=${id(row.id)}&history=1`}>{row.created_at ? new Date(row.created_at).toLocaleString("zh-CN") : "历史执行"} · {row.input?.query || "已保存的结果"} →</a></p>)}{executionsNext !== null && <button className="ghost" disabled={op.busy} onClick={() => void op.perform(() => loadExecutions(true))}>更早的执行</button>}</details>}
    <section className={`work-list ${chosen.current ? "work-list-secondary" : ""}`} aria-label="我的工作"><div className="section-heading"><h2>最近查询与收到的答复</h2><button className="ghost" disabled={historyBusy} onClick={() => void loadHistory(false, true)}>更新记录</button></div>{historyError && <p role="alert">{historyError}</p>}<table><thead><tr><th>问题</th><th>进度</th><th>时间</th><th>操作</th></tr></thead><tbody>{groups.map(row => <tr key={row.id}><td><strong>{workTitle(row.input?.query) || (row.status === "access_limited" ? "受限工作" : "收到的回答")}</strong><small>{row.input ? "本人查询" : "仅分享的单条回答"}</small></td><td>{statusNames[row.status] || "需要核对"}</td><td>{row.created_at ? new Date(row.created_at).toLocaleString("zh-CN") : "—"}</td><td><button className="text-button" disabled={op.busy || !!unknown} onClick={() => void op.perform(() => openTask(row))}>{row.status === "waiting_input" ? "补充条件" : row.status === "succeeded" ? "查看结果" : "查看工作"} →</button></td></tr>)}</tbody></table>{!groups.length && !historyBusy && !historyError && <p className="empty">暂无工作。从上方描述一项需求开始。</p>}{historyBusy && <p role="status">正在读取工作…</p>}{historyNext !== null && <button className="ghost" disabled={historyBusy} onClick={() => void loadHistory(true)}>加载更早工作</button>}{profile!.capabilities.includes("task.share") && <details className="share-identity"><summary>需要接收同事分享的答复？</summary><p className="locator">请将此账号编号提供给分享人：<code>{owner}</code>。接收分享仍需具备原资料访问权限。</p></details>}</section>
    {task?.input && task.conversation_id && profile!.capabilities.includes("feedback.read_own") && <details id="query-result-feedback"><summary>反馈资料问题与查看处理进度</summary><FeedbackPanel key={task.id} origin={{ task_id: task.id }} onQuery={taskId => openTask({ id: taskId, status: "pending" })}/></details>}
  </div>;
}

function workTitle(value?: string) { return value === "SYNTHETIC-1 startup and pressure" ? "SYNTHETIC-1：开机步骤与压力要求" : value; }

function QueryResult({ result, taskId, hasFeedback }: { result: Result; taskId: string; hasFeedback: boolean }) {
  const model = result.task?.result?.model;
  const coverage = result.coverage ?? result.task?.result?.coverage;
  const missing = coverage?.items?.filter(item => item.state === "missing") ?? [];
  const missingLabels = missing.map(item => item.label?.trim()).filter((label): label is string => Boolean(label));
  const missingCount = Math.max(missing.length, coverage?.missing_count ?? 0, coverage?.missing_item_refs?.length ?? 0);
  const notices = [...new Set(result.warnings.map(code => warnings[code] || "部分内容暂不可核验，请查看原文或反馈。"))];
  const usable = renderableResults.has(result.result_status);
  const guidance = resultGuidance[result.result_status] || (!usable ? "暂未找到可支持此问题的完整依据。" : "");
  const answer = usable && <VerifiedAnswer result={result} />;
  return <div className="result-content">
    {model && <p>本次采用的设备型号 {model}</p>}
    {guidance && <p className="muted">{guidance}</p>}
    {answer}
    {missingCount > 0 && <section className="warning query-result-coverage" aria-label="依据缺项">
      <strong>{missingCount} 项请求缺少可核对依据</strong>
      {!!missingLabels.length && <ul>{[...new Set(missingLabels)].map(label => <li key={label}>{label}</li>)}</ul>}
    </section>}
    <ul className="warning query-result-warnings" aria-label="核对提示">
      {notices.map(notice => <li key={notice}>{notice}</li>)}
      <li>设备操作须由业务人员复核，并以适用的企业操作规程为准。</li>
    </ul>
    {!!result.evidence.length && <details className="result-sources"><summary>查看全部 {result.evidence.length} 处来源</summary>{result.evidence.map((evidence, index) => <EvidenceCard key={evidence.id || evidence.version.id + "-" + index} evidence={evidence} taskId={taskId} />)}</details>}
    {result.evidence.length === 0 && <p className="muted">本次查询没有可打开的原文出处。</p>}
    {hasFeedback && (missingCount > 0 || !usable) && <a href="#query-result-feedback">反馈本次查询遇到的问题</a>}
  </div>;
}

function VerifiedAnswer({ result }: { result: Result }) {
  if (result.answer) return <div className="answer">{result.answer}</div>;
  const byId = new Map(result.claims?.map(claim => [claim.claim_id, claim.text]));
  const sections = result.sections?.map(section => ({
    ...section, texts: section.claim_ids.map(ref => byId.get(ref)).filter((text): text is string => Boolean(text)),
  })).filter(section => section.texts.length > 0);
  if (!sections?.length) {
    return result.result_status === "formal" ? <p className="muted">整理答复暂不可读取，请查看下方原文出处。</p> : null;
  }
  return <div className="answer">{sections.map((section, index) => <section key={index}>
    <h3>{section.heading}</h3>
    {section.texts.map((claim, claimIndex) => <p key={claimIndex}>{claim}</p>)}
  </section>)}</div>;
}
