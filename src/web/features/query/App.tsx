import { useEffect, useRef, useState } from "react";
import { api, errorMessage, HttpError, id, StaleSessionError } from "../../api";
import { AnswerSharing } from "./AnswerSharing";
import { formValues, Status, text, useOperation, Workspace } from "../../components";
import { useSession } from "../../session";
import { FeedbackPanel } from "../feedback";
import { EvidenceCard } from "./EvidenceCard";
import type { Evidence } from "./EvidenceCard";

type QueryInput = { query: string; model: string | null; mode: "retrieve" };
type Task = { id: string; kind: string; status: string; input?: QueryInput | null; next_action?: string; conversation_id?: string; retry_eligible?: boolean };
type InputRequest = { id: string; revision: number; status: string; reason: string };
type ResultCoverage = { missing_count?: number; missing_item_refs?: string[]; items?: { label?: string; state: string }[] };
type Result = {
  answer: string | null; result_status: string; warnings: string[]; evidence: Evidence[];
  answer_snapshot_id?: string | null;
  sections?: { heading: string; claim_ids: string[] }[];
  claims?: { claim_id: string; text: string }[];
  coverage?: ResultCoverage | null;
  task?: { id?: string; input?: QueryInput | null; result?: { model?: string; coverage?: ResultCoverage | null } };
};
type Saved = { owner: string; task: string | null; pendingSubmit: QueryInput | null; pendingInput: { task: string; request: string; revision: number; model: string } | null };
const storage = "dc-workspace-query";
const warnings: Record<string, string> = { index_stale: "资料更新后仍在准备查询", index_not_ready: "资料尚未准备好查询", partial_index: "部分资料尚未准备完成", evidence_pending_review: "原文内容仍待核验", content_pending_review: "手册仍待核验", publication_not_ready: "手册尚未满足正式使用条件", insufficient_evidence: "缺少足够依据", access_limited: "资料访问权限不足", model_or_evidence_mismatch: "型号或适用资料尚不明确" };
const resultBadges: Record<string, string> = {
  formal: "已找到可核对依据",
  extractive: "原文摘录",
  partial: "依据部分覆盖",
  needs_review: "待人工核对",
};
const resultGuidance: Record<string, string> = {
  extractive: "请结合原文上下文核对下方摘录。",
  partial: "已找到部分依据，其余内容仍需核对。",
  needs_review: "现有依据仍需复核，暂未生成可使用的答复。",
};
const renderableResults = new Set(["formal", "extractive", "partial"]);
const retryPath = /^\/tasks\/([^/]+)\/retry$/;

export function QueryApp() {
  const manual = new URLSearchParams(location.search).get("manual");
  return <Workspace title={manual ? "手册查阅" : "设备资料查询"} eyebrow="DEVICE QUERY" intro={manual ? "查看原文版本，提交和跟进资料问题。" : "只根据已发布、已核验且当前有权访问的手册回答。"}>
    {manual ? <div className="panel"><FeedbackPanel origin={{ document_version_id: manual }}
      onQuery={taskId => location.assign("/workspace/query?task=" + id(taskId))} /></div> : <QueryWorkspace />}
  </Workspace>;
}
function QueryWorkspace() {
  const { profile } = useSession(), owner = profile!.user.id, op = useOperation();
  const [query, setQuery] = useState(""), [model, setModel] = useState(""), [result, setResult] = useState<Result | null>(null), [history, setHistory] = useState<Task[]>([]), [request, setRequest] = useState<InputRequest | null>(null), [status, setStatus] = useState("提交问题后，这里会显示整理说明、版本状态和可核对的原文片段。"), [badge, setBadge] = useState("等待查询"), [recoverable, setRecoverable] = useState(false);
  const saved = useRef<Saved>({ owner, task: null, pendingSubmit: null, pendingInput: null }), active = useRef(true), timer = useRef<ReturnType<typeof setTimeout> | null>(null), sequence = useRef(0);
  const [feedbackTask, setFeedbackTask] = useState<string | null>(null);
  const [retryTarget, setRetryTarget] = useState<{ task: string; etag: string } | null>(null);
  const [historyNext, setHistoryNext] = useState<number | null>(null), [historyBusy, setHistoryBusy] = useState(false), [historyError, setHistoryError] = useState("");
  const historyOpen = useRef(false), historySequence = useRef(0);
  const pendingRetry = api.pendingCommands().find(command => command.method === "POST" && retryPath.test(command.path));
  function save() { if (api.owner === owner) sessionStorage.setItem(storage, JSON.stringify(saved.current)); }
  function clearTimer() { if (timer.current) clearTimeout(timer.current); }
  async function loadHistory(more = false) {
    const seq = ++historySequence.current, offset = more ? historyNext : 0;
    if (offset === null) return;
    setHistoryBusy(true); setHistoryError("");
    try {
      const response = await api.get<{ items: Task[]; next_offset: number | null }>(`/tasks?limit=5&offset=${offset}`);
      if (!active.current || seq !== historySequence.current) return;
      const rows = response.items.filter(task => task.kind === "query" || task.status === "access_limited");
      setHistory(previous => [...new Map((more ? [...previous, ...rows] : rows).map(task => [task.id, task])).values()]);
      setHistoryNext(response.next_offset);
    } catch (error) {
      if (active.current && seq === historySequence.current && !(error instanceof StaleSessionError)) setHistoryError(errorMessage(error));
    } finally { if (active.current && seq === historySequence.current) setHistoryBusy(false); }
  }
  function showError(error: unknown) {
    if (!active.current || error instanceof StaleSessionError) return;
    setResult(null); setRequest(null); setFeedbackTask(null); setRetryTarget(null); setBadge("查询未完成"); setStatus(errorMessage(error) + " 原任务已保留，可刷新状态。"); setRecoverable(true);
  }
  async function poll() {
    clearTimer(); const taskId = saved.current.task, seq = ++sequence.current;
    if (!taskId) return;
    const current = () => active.current && saved.current.task === taskId && sequence.current === seq;
    setResult(null); setRequest(null); setRetryTarget(null); setFeedbackTask(null); setRecoverable(true);
    const progress = await api.get<Task>(`/tasks/${id(taskId)}/status`); if (!current()) return;
    if (progress.status === "access_limited") {
      setStatus("访问条件已变化，请确认资料权限或联系管理员。"); setBadge("查询尚未完成");
      return;
    }
    if (["pending", "running"].includes(progress.status)) {
      setStatus(progress.status === "pending" ? "查询已保存，正在等待执行。关闭或刷新不会重复提交。" : "查询处理中，关闭或刷新网页不会重复提交。"); setBadge(progress.status === "pending" ? "查询已排队" : "正在查询");
      timer.current = setTimeout(() => void poll().catch(showError), 1000);
      return;
    }
    const { data: task, etag } = await api.request<Task>(`/tasks/${id(taskId)}`); if (!current()) return;
    if (task.retry_eligible && etag) setRetryTarget({ task: taskId, etag });
    setFeedbackTask(task.conversation_id && profile!.capabilities.includes("feedback.read_own") ? task.id : null);
    if (["pending", "running"].includes(task.status)) {
      setStatus(task.status === "pending" ? "查询已保存，正在等待执行。关闭或刷新不会重复提交。" : "查询处理中，关闭或刷新网页不会重复提交。"); setBadge(task.status === "pending" ? "查询已排队" : "正在查询");
      timer.current = setTimeout(() => void poll().catch(showError), 1000);
    } else if (task.status === "waiting_input") {
      const response = await api.get<{ items: InputRequest[] }>(`/tasks/${id(taskId)}/input-requests`); if (!current()) return;
      const open = response.items.find(item => item.status === "open") || null;
      setRequest(open); setStatus(open?.reason || "需要补充设备条件"); setBadge("等待补充");
    } else if (task.status === "succeeded") {
      const response = await api.get<Result>(`/tasks/${id(taskId)}/result`); if (!current()) return;
      setResult(response); setBadge(resultBadges[response.result_status] || "需要补充或人工复核"); setStatus("结果已保存，请逐条核对版本、出处和警告。"); saved.current.pendingInput = null; save(); if (historyOpen.current) await loadHistory();
    } else { setStatus(task.next_action || "访问条件已变化，请确认资料权限或联系管理员。"); setBadge("查询尚未完成"); }
  }
  async function submitOriginal() {
    const pending = saved.current.pendingSubmit; if (!pending) return;
    let response: { task_id: string };
    try { response = await api.mutate<{ task_id: string }>("/knowledge/queries", pending); }
    catch (error) { if (error instanceof HttpError && error.status < 500) { saved.current.pendingSubmit = null; save(); } throw error; }
    if (!active.current) return;
    saved.current.task = response.task_id; saved.current.pendingSubmit = null; save(); await poll();
  }
  async function sendInput() {
    const pending = saved.current.pendingInput; if (!pending) return;
    try { await api.mutate(`/tasks/${id(pending.task)}/input-requests/${id(pending.request)}/responses`, { model: pending.model }, `"${pending.revision}"`); }
    catch (error) { if (error instanceof HttpError && error.status < 500) { saved.current.pendingInput = null; save(); } throw error; }
    saved.current.pendingInput = null; save(); await poll();
  }
  async function retryOriginal() {
    if (pendingRetry) await api.recoverCommand(pendingRetry);
    else if (retryTarget) await api.mutate(`/tasks/${id(retryTarget.task)}/retry`, { reason: "服务恢复后重试原查询" }, retryTarget.etag);
    await poll();
  }
  useEffect(() => {
    active.current = true;
    try { const stored = JSON.parse(sessionStorage.getItem(storage) || "null"); if (stored?.owner === owner) saved.current = stored; } catch { sessionStorage.removeItem(storage); }
    // Re-authentication may clear the visible task projection while the original
    // command still has an unknown outcome. Reuse its exact inputs and key.
    const commands = api.pendingCommands();
    const pendingQuery = commands.find(command => command.path === "/knowledge/queries");
    if (pendingQuery?.replayBody) saved.current.pendingSubmit = JSON.parse(pendingQuery.replayBody);
    const pendingInput = commands.find(command => /^\/tasks\/[^/]+\/input-requests\/[^/]+\/responses$/.test(command.path));
    if (pendingInput?.replayBody) { const parts = pendingInput.path.split("/"); saved.current.task = decodeURIComponent(parts[2]); saved.current.pendingInput = { task: saved.current.task, request: decodeURIComponent(parts[4]), revision: Number(pendingInput.match?.replaceAll('"', "")), model: JSON.parse(pendingInput.replayBody).model }; }
    const retry = commands.find(command => command.method === "POST" && retryPath.test(command.path));
    if (retry) { saved.current.task = decodeURIComponent(retry.path.match(retryPath)![1]); save(); }
    const linkedTask = new URLSearchParams(location.search).get("task");
    if (linkedTask && !retry && !saved.current.pendingSubmit && !saved.current.pendingInput) {
      saved.current.task = linkedTask;
      save();
      historyReplace();
    }
    if (saved.current.pendingSubmit) { setQuery(saved.current.pendingSubmit.query); setModel(saved.current.pendingSubmit.model || ""); setStatus("上次提交结果待核对，请恢复原提交读取回执。"); setRecoverable(true); }
    if (saved.current.task && !saved.current.pendingSubmit) void poll().catch(showError);
    return () => { active.current = false; sequence.current++; historySequence.current++; clearTimer(); };
  }, [owner]);
  function historyReplace() { window.history.replaceState(null, "", "/workspace/query"); }
  return <section className="workspace"><div className="panel query-panel"><div className="section-heading"><div><p className="eyebrow">KNOWLEDGE QUERY</p><h2>描述你要完成的操作</h2></div></div><fieldset className="operation-fields" disabled={op.busy} aria-busy={op.busy}>
    {profile!.capabilities.includes("knowledge.query") ? <form className="stack" onSubmit={event => { event.preventDefault(); void op.perform(async () => {
      if (pendingRetry) throw Error("请先恢复原重试回执。");
      const payload: QueryInput = { query, model: model || null, mode: "retrieve" };
      if (saved.current.pendingSubmit && JSON.stringify(saved.current.pendingSubmit) !== JSON.stringify(payload)) throw Error("上次提交结果待核对，请先恢复原提交。");
      saved.current.pendingSubmit ||= payload; save(); clearTimer(); setResult(null); setRequest(null); setStatus("正在检查适用版本和权限…");
      try { await submitOriginal(); } catch (error) {
        if (error instanceof HttpError && error.status === 429) { setBadge("尚未提交"); setStatus("当前查询较多，本次未创建新任务。请稍后再提交。"); setRecoverable(Boolean(saved.current.task)); }
        else showError(error);
        throw error;
      }
    }); }}><label>设备型号（可选）<input value={model} onChange={event => setModel(event.target.value)} placeholder="例如 H3C ER2100" maxLength={200} /></label><label>问题<textarea value={query} onChange={event => setQuery(event.target.value)} rows={4} required placeholder="例如：如何开机？日常维护有哪些注意事项？" /></label><button disabled={Boolean(pendingRetry)}>提交查询</button></form> : <p role="alert">当前账号没有资料查询权限。</p>}
    <p className="status muted" role="status">{status}</p>
    {recoverable && <button className="ghost" type="button" onClick={() => void op.perform(async () => { try { await (saved.current.pendingSubmit ? submitOriginal() : saved.current.pendingInput ? sendInput() : poll()); } catch (error) { showError(error); throw error; } })}>{saved.current.pendingSubmit ? "恢复原提交" : saved.current.pendingInput ? "恢复原补充回执" : "刷新原查询状态"}</button>}
    {(retryTarget || pendingRetry) && <button className="ghost" type="button" onClick={() => void op.perform(retryOriginal)}>{pendingRetry ? "恢复原重试回执" : "重试原查询"}</button>}
    {request && <form className="stack input-form" onSubmit={event => { event.preventDefault(); const values = formValues(event); void op.perform(async () => {
      const pending = { task: saved.current.task!, request: request.id, revision: request.revision, model: text(values, "model") };
      if (saved.current.pendingInput && JSON.stringify(saved.current.pendingInput) !== JSON.stringify(pending)) throw Error("原补充结果待核对，请先恢复原补充回执。");
      saved.current.pendingInput ||= pending; save(); try { await sendInput(); } catch (error) { showError(error); throw error; }
    }); }}><label>补充设备型号<input name="model" required maxLength={200} /></label><button>补充并继续原任务</button></form>}
    <details onToggle={event => { historyOpen.current = event.currentTarget.open; if (historyOpen.current) void loadHistory(); }}><summary>我的查询记录</summary><ul>{history.map(task => <li key={task.id}><button className="ghost" type="button" disabled={Boolean(pendingRetry)} onClick={() => void op.perform(async () => { if (saved.current.pendingSubmit || saved.current.pendingInput || pendingRetry) throw Error("请先核对上次操作回执。"); saved.current.task = task.id; save(); await poll(); })}>{task.input?.query || "已保存的查询"}</button></li>)}</ul>{historyBusy && <p role="status">正在读取查询记录…</p>}{!history.length && !historyBusy && !historyError && <p className="muted">暂无查询记录。</p>}{historyError && <p role="alert">{historyError}</p>}<button type="button" className="ghost" disabled={historyBusy} onClick={() => void loadHistory()}>刷新查询记录</button>{historyNext !== null && <button type="button" className="ghost" disabled={historyBusy} onClick={() => void loadHistory(true)}>加载更多查询记录</button>}{profile!.capabilities.includes("task.share") && <p className="locator">用于接收分享的用户编号 <code>{owner}</code></p>}</details><Status message={op.message} bad={op.bad} /></fieldset></div>
    <section className="panel result-panel">
      <div className="section-heading"><div><p className="eyebrow">EVIDENCE</p><h2>结果与出处</h2></div><span className="badge">{badge}</span></div>
      {result ? <QueryResult result={result} taskId={result.task?.id || saved.current.task || ""} hasFeedback={Boolean(feedbackTask)} />
        : <p className="result-content empty">提交问题后，这里会显示整理说明、版本状态和可核对的原文片段。</p>}
      {result?.task?.id && result.task.input && result.answer_snapshot_id && renderableResults.has(result.result_status)
        && profile!.capabilities.includes("task.share") && <AnswerSharing key={result.answer_snapshot_id}
          taskId={result.task.id} snapshotId={result.answer_snapshot_id} />}
      {feedbackTask && <div id="query-result-feedback">
        <FeedbackPanel key={feedbackTask} origin={{ task_id: feedbackTask }} onQuery={async taskId => {
          setFeedbackTask(null); saved.current.task = taskId; save(); await poll();
        }} />
      </div>}
    </section>
  </section>;
}

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
    {result.evidence.map((evidence, index) => <EvidenceCard key={evidence.id || evidence.version.id + "-" + index} evidence={evidence} taskId={taskId} />)}
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
