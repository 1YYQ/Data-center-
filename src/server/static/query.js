let stored = null;
try { stored = JSON.parse(sessionStorage.getItem("dc-query-session") || "null"); } catch { sessionStorage.removeItem("dc-query-session"); }
const state = { csrf: stored?.csrf || null, owner: stored?.owner || null, task: stored?.task || null, pendingSubmit: stored?.pendingSubmit || null, pendingInput: stored?.pendingInput || null, inputRequest: null, timer: null };
const $ = (selector) => document.querySelector(selector);
const save = () => sessionStorage.setItem("dc-query-session", JSON.stringify({ csrf: state.csrf, owner: state.owner, task: state.task, pendingSubmit: state.pendingSubmit, pendingInput: state.pendingInput }));

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.csrf && options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
  const response = await fetch(path, { ...options, headers });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new Error(body?.message || "请求失败，请刷新原任务状态后重试");
  return body;
}
const warnings = { index_stale: "资料更新后仍在准备查询", index_not_ready: "资料尚未准备好查询", partial_index: "部分资料尚未准备完成", evidence_pending_review: "原文内容仍待核验", content_pending_review: "手册仍待核验", publication_not_ready: "手册尚未满足正式使用条件", insufficient_evidence: "缺少足够依据", access_limited: "资料访问权限不足", model_or_evidence_mismatch: "型号或适用资料尚不明确" };
function renderResult(payload) {
  const result = $("#result");
  $("#result-badge").textContent = payload.result_status === "formal" ? "已找到可核对依据" : "需要补充或人工复核";
  result.classList.remove("empty"); result.replaceChildren();
  if (payload.task?.result?.model) { const condition = document.createElement("p"); condition.textContent = "本次采用的设备型号：" + payload.task.result.model; result.append(condition); }
  if (payload.answer) { const answer = document.createElement("div"); answer.className = "answer"; answer.textContent = payload.answer; result.append(answer); }
  const note = document.createElement("p"); note.className = "warning";
  note.textContent = [...(payload.warnings || []).map((code) => warnings[code] || "部分依据需要人工核对"), "设备操作须由业务人员复核，不能直接作为设备执行指令。"].join("；");
  result.append(note);
  for (const item of payload.evidence || []) {
    const evidence = document.createElement("article"); evidence.className = "evidence";
    const title = document.createElement("h3"); title.textContent = item.document_title + " · " + (item.version.revision_label || "第 " + item.version.version_no + " 版") + (item.version.publish_status === "superseded" ? "（历史版本）" : "");
    const quote = document.createElement("blockquote"); quote.textContent = item.quote;
    const link = document.createElement("a");
    const page = Number.isInteger(item.locator?.file_page) && item.locator.file_page > 0 ? item.locator.file_page : null;
    link.href = "/documents/" + encodeURIComponent(item.version.document_id) + "/versions/" + encodeURIComponent(item.version.id) + "/file" + (page ? "#page=" + page : "");
    link.target = "_blank"; link.rel = "noopener";
    link.textContent = "核对原文" + (page ? " · PDF 第 " + page + " 页" : "") + (item.locator?.printed_page ? " · 印刷页码 " + item.locator.printed_page : "") + (item.locator?.section ? " · " + item.locator.section : "");
    evidence.append(title, quote, link); result.append(evidence);
  }
}
async function showHistory() {
  const { items } = await api("/tasks"); $("#task-history").replaceChildren();
  for (const task of items.filter((item) => item.kind === "query" || item.status === "access_limited")) {
    const row = document.createElement("li"), button = document.createElement("button"); button.type = "button"; button.className = "ghost";
    button.textContent = task.input?.query || "已保存的查询";
    button.addEventListener("click", () => { state.task = task.id; save(); poll().catch(showError); });
    row.append(button); $("#task-history").append(row);
  }
}
function showError(error) {
  $("#query-state").textContent = (error instanceof TypeError ? "网络连接异常" : error.message) + "。原任务已保留，可刷新状态。";
  $("#result").replaceChildren();
  $("#result-badge").textContent = "查询未完成"; $("#refresh-task").classList.remove("hidden");
}
async function poll() {
  clearTimeout(state.timer); if (!state.task) return;
  const taskId = state.task;
  const task = await api("/tasks/" + taskId);
  if (state.task !== taskId) return; $("#input-form").classList.add("hidden"); $("#refresh-task").classList.remove("hidden");
  if (state.displayedTask !== taskId) { $("#result").replaceChildren(); state.displayedTask = taskId; }
  if (["pending", "running"].includes(task.status)) {
    $("#query-state").textContent = "查询处理中，关闭或刷新网页不会重复提交。"; $("#result-badge").textContent = "正在查询";
    state.timer = setTimeout(() => poll().catch(showError), 1000);
  } else if (task.status === "waiting_input") {
    const { items } = await api("/tasks/" + taskId + "/input-requests"); if (state.task !== taskId) return; state.inputRequest = items.find((item) => item.status === "open");
    $("#query-state").textContent = state.inputRequest?.reason || "需要补充设备条件"; $("#result-badge").textContent = "等待补充"; $("#input-form").classList.remove("hidden");
  } else if (task.status === "succeeded") {
    const result = await api("/tasks/" + taskId + "/result"); if (state.task !== taskId) return; renderResult(result); state.pendingInput = null; save();
    $("#query-state").textContent = "结果已保存，请逐条核对版本、出处和警告。"; await showHistory();
  } else {
    $("#query-state").textContent = task.next_action || "访问条件已变化，请确认资料权限或联系管理员。";
    $("#result-badge").textContent = "查询尚未完成"; $("#result").replaceChildren();
  }
}
async function submitOriginal() {
  const pending = state.pendingSubmit;
  const task = await api("/knowledge/queries", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": pending.key }, body: pending.body });
  state.task = task.task_id; state.pendingSubmit = null; save(); await poll();
}
async function enter(user) {
  if (state.owner && state.owner !== user.id) state.task = state.pendingSubmit = state.pendingInput = null;
  state.owner = user.id; save(); $("#session").textContent = user.display_name; $("#login-panel").classList.add("hidden"); $("#workspace").classList.remove("hidden");
  const me = await api("/me"), permitted = me.capabilities.includes("knowledge.query"); $("#query-form").classList.toggle("hidden", !permitted);
  if (!permitted) $("#query-state").textContent = "当前账号没有资料查询权限。";
  await showHistory(); if (state.task) await poll(); else if (state.pendingSubmit) $("#refresh-task").classList.remove("hidden");
}
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("#login-error").textContent = "";
  try {
    const payload = await api("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(new FormData(event.target))) });
    state.csrf = payload.csrf_token; await enter(payload.user); event.target.reset();
  } catch (error) { $("#login-error").textContent = error.message; }
});
$("#logout").addEventListener("click", async () => {
  try { await api("/auth/logout", { method: "POST" }); } finally { sessionStorage.removeItem("dc-query-session"); location.reload(); }
});
$("#query-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.target.querySelector("button"); button.disabled = true;
  const values = Object.fromEntries(new FormData(event.target)), body = JSON.stringify({ query: values.query, model: values.model || null, mode: "retrieve" });
  try {
    if (state.pendingSubmit && state.pendingSubmit.body !== body) throw new Error("上次提交结果待核对，请先恢复原提交");
    state.pendingSubmit ||= { key: crypto.randomUUID(), body }; save(); await submitOriginal();
  } catch (error) { showError(error); } finally { button.disabled = false; }
});
$("#input-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.target.querySelector("button"); button.disabled = true;
  try {
    const request = state.inputRequest; if (!request) throw new Error("请先刷新当前补充请求");
    const body = JSON.stringify({ model: new FormData(event.target).get("model") });
    if (state.pendingInput && (state.pendingInput.body !== body || state.pendingInput.request !== request.id)) throw new Error("原补充结果待核对，请先刷新原任务");
    state.pendingInput ||= { key: crypto.randomUUID(), body, request: request.id, revision: request.revision }; save();
    const pending = state.pendingInput;
    await api("/tasks/" + state.task + "/input-requests/" + pending.request + "/responses", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": pending.key, "If-Match": '"' + pending.revision + '"' }, body: pending.body });
    state.pendingInput = null; save(); await poll();
  } catch (error) { showError(error); } finally { button.disabled = false; }
});
$("#refresh-task").addEventListener("click", () => (state.pendingSubmit ? submitOriginal() : poll()).catch(showError));
if (state.csrf) api("/me").then((me) => enter(me.user)).catch(() => sessionStorage.removeItem("dc-query-session"));
