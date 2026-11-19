let csrfToken = null;
let currentUser = null;
let approvals = [];
let selected = null;
let verificationOptions = { authority_bases: [], evidence: [], proof_refs: [], revision: null };
let uploaded = [];
const $ = selector => document.querySelector(selector);
const operationKeys = new Map();
function keyFor(name) { if (!operationKeys.has(name)) operationKeys.set(name, crypto.randomUUID()); return operationKeys.get(name); }
function doneKey(name) { operationKeys.delete(name); }
function errorText(data) { return data?.detail?.message || data?.detail || data?.message || "操作未完成，请检查输入或刷新记录。"; }
function clearForLostAccess() {
  csrfToken = null; currentUser = null; approvals = []; selected = null; uploaded = [];
  verificationOptions = { authority_bases: [], evidence: [], proof_refs: [], revision: null };
  $("#workspace").classList.add("hidden"); $("#login").classList.remove("hidden"); $("#identity").textContent = "";
  $("#login-error").textContent = "当前登录或授权已失效，页面中的记录已清除。请重新登录。"; $("#login-form").reset(); clearDetail();
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== "GET" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, { ...options, headers, cache: "no-store", credentials: "same-origin" });
  const data = await response.json().catch(() => null);
  if (!response.ok) { const error = new Error(errorText(data)); error.status = response.status; if ((response.status === 401 || response.status === 403) && currentUser) clearForLostAccess(); throw error; }
  return { data, etag: response.headers.get("ETag") };
}
function setStatus(message, bad = false) { const node = $("#status"); node.textContent = message; node.className = `status ${bad ? "bad" : "success"}`; }
function recordStatus(row) {
  if (row.revoked) return ["已撤销", "state-revoked"];
  if (row.current === false && row.verification_state === "verified") return ["当前不可用", "state-revoked"];
  const map = { verified: ["已核查", "state-verified"], rejected: ["未通过", "state-rejected"], unverified: ["待核查", "state-unverified"], pending: ["待核查", "state-unverified"] };
  return map[row.verification_state] || [row.status === "approved" ? "已核查" : "待核查", row.status === "approved" ? "state-verified" : "state-unverified"];
}
function appendText(parent, tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; node.textContent = value == null || value === "" ? "未登记" : String(value); parent.append(node); return node; }
function formatTime(value) { if (!value) return "未登记"; const date = new Date(value); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN"); }
function renderList() {
  const box = $("#approval-list"); box.replaceChildren(); const filter = $("#filter").value;
  const rows = approvals.filter(row => filter === "all" || (filter === "revoked" ? row.revoked === true : filter === "unavailable" ? row.current === false && !row.revoked : row.verification_state === filter));
  $("#empty-list").classList.toggle("hidden", rows.length > 0);
  for (const row of rows) {
    const button = document.createElement("button"); button.type = "button"; button.className = "approval-item"; button.setAttribute("aria-current", String(selected?.id === row.id)); button.addEventListener("click", () => openRecord(row.id));
    const main = document.createElement("span"); main.className = "item-main"; appendText(main, "span", "item-subject", row.applicant); const [label, cls] = recordStatus(row); appendText(main, "span", `state-pill ${cls}`, label); button.append(main);
    appendText(button, "span", "item-meta", `${row.decision === "approved" ? "外部批准" : row.decision === "rejected" ? "外部拒绝" : "决定未登记"} · ${formatTime(row.approved_at)}`); appendText(button, "span", "item-id", row.id); box.append(button);
  }
}
function renderFacts(row) {
  const box = $("#facts"); box.replaceChildren(); const scope = row.scope || {}, person = row.actual_approver || {};
  const facts = [["被授权用户", row.applicant], ["系统外审批人", person.identity_label], ["审批职责", person.responsibility],
    ["实际决定", row.decision === "approved" ? "批准" : row.decision === "rejected" ? "拒绝" : "未登记"], ["实际决定时间", formatTime(row.approved_at)], ["系统登记人", row.recorded_by],
    ["知识库 / 文档范围", `${scope.knowledge_base_id || "未登记"} / ${scope.document_id || "整个知识库"}`], ["资料范围", `${scope.level || "未登记"} · ${scope.category || "未登记"} · ${scope.need_class || "未登记"}`],
    ["允许操作", (row.operations || []).join("、") || "未登记"], ["授权期限", `${formatTime(row.valid_from)} 至 ${formatTime(row.valid_until)}`], ["登记来源", row.evidence_locator], ["核查人 / 时间", `${row.verified_by || "未登记"} / ${formatTime(row.verified_at)}`], ["外部撤销回执", row.revoked ? row.revocation_id : "无撤销记录"]];
  for (const [label, value] of facts) { const item = document.createElement("div"); item.className = "fact"; appendText(item, "span", "fact-label", label); appendText(item, "span", "fact-value", value); box.append(item); }
}
function renderEvidence(row) {
  const box = $("#evidence-list"); box.replaceChildren(); const refs = verificationOptions.proof_refs || row.proof_refs || [];
  const candidates = $("#evidence-candidates"); candidates.replaceChildren();
  for (const item of verificationOptions.evidence) {
    const line = document.createElement("div"); line.className = "evidence-row";
    const label = document.createElement("label"); label.className = "evidence-choice"; const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.name = "evidence-ref"; checkbox.value = item.id; checkbox.checked = refs.includes(item.id); checkbox.disabled = row.verification_state === "verified" || row.status !== "pending"; label.append(checkbox);
    const title = document.createElement("span"); title.textContent = `${item.source_description || "依据材料"} · ${item.id}`; label.append(title); line.append(label);
    appendText(line, "span", "", item.attested ? "受控导入材料" : "未核实 · 不能支持通过");
    const link = document.createElement("a"); link.href = `/admin/approval-evidence/${encodeURIComponent(item.id)}/file`; link.textContent = "查看附件"; link.target = "_blank"; link.rel = "noopener noreferrer"; line.append(link); candidates.append(line);
    if (refs.includes(item.id)) { const receipt = document.createElement("p"); receipt.className = "inline-note"; receipt.textContent = `${item.id} · ${item.attested ? "当前已关联的受控材料" : "当前已关联但尚未核实"}`; box.append(receipt); }
  }
  for (const ref of refs.filter(id => !verificationOptions.evidence.some(item => item.id === id))) appendText(box, "p", "inline-note", `已关联引用 ${ref}；服务端未返回可读取的材料状态。`);
  for (const item of uploaded.filter(x => x.owner_id === row.id && !refs.includes(x.id) && !verificationOptions.evidence.some(y => y.id === x.id))) appendText(box, "p", "inline-note", `${item.id} · ${item.source_description || "新上传材料"} · 未关联`);
  if (!verificationOptions.evidence.length) appendText(candidates, "p", "inline-note", "没有可供此记录关联的材料。");
  const editable = row.verification_state !== "verified" && row.status === "pending";
  $("#evidence-link-form").classList.toggle("hidden", !editable); $("#link-evidence-button").disabled = !editable || !verificationOptions.evidence.length;
}
function fillOptions(select, options, chosen = "") {
  select.replaceChildren(); const first = document.createElement("option"); first.value = ""; first.textContent = options.length ? "选择服务端核验的职责依据" : "没有有效匹配的职责依据"; select.append(first);
  for (const basis of options) { const option = document.createElement("option"); option.value = basis.id; option.textContent = `${basis.identity_label || "审批人"} · ${basis.id}`; option.selected = basis.id === chosen; select.append(option); }
  select.disabled = options.length === 0;
}
function verificationBlocked(row) {
  const reasons = []; if (row.decision !== "approved") reasons.push("外部决定不是批准");
  if (row.verification_state === "verified" || row.status === "approved") reasons.push("本记录已核查；变更依据请追加替代登记");
  if (!row.approved_at || !row.valid_until || !row.valid_from || new Date(row.valid_until) <= new Date(row.valid_from)) reasons.push("批准时间或完整有效期限缺失");
  const scope = row.scope || {}; if (!(scope.knowledge_base_id || scope.document_id) || !scope.level || !scope.category || !scope.need_class || !row.operations?.length) reasons.push("授权范围、资料等级/类别/用途或允许操作不完整");
  const refs = verificationOptions.proof_refs || row.proof_refs || []; const good = new Set(verificationOptions.evidence.filter(x => x.attested && x.linked).map(x => x.id));
  if (!refs.length || refs.some(ref => !good.has(ref))) reasons.push("证据缺失、未关联或未受控；普通上传材料不能支持核查通过");
  if (!verificationOptions.authority_bases.length) reasons.push("没有服务端确认的当前有效审批职责依据"); return reasons;
}
function updateVerifyGate() {
  const reasons = verificationBlocked(selected || {}); const locked = selected?.verification_state === "verified" || selected?.status === "approved"; const blocked = locked || ($("#decision").value === "verified" && reasons.length > 0); const node = $("#verification-block");
  node.textContent = blocked ? `当前不能提交“核对通过”：${reasons.join("；")}。` : ""; node.classList.toggle("hidden", !blocked); $("#verify-button").disabled = blocked || !verificationOptions.authority_bases.length;
  $("#verify-button").textContent = $("#decision").value === "verified" ? "提交核查记录" : "登记不通过结论";
}
async function loadApprovals() {
  approvals = []; let cursor = null;
  do { const query = new URLSearchParams({ limit: "100" }); if (cursor) query.set("cursor", cursor); const result = await api(`/admin/business-approvals?${query}`); approvals.push(...(result.data.items || [])); cursor = result.data.next_cursor || null; } while (cursor);
  renderList();
  if (selected && approvals.some(x => x.id === selected.id)) await openRecord(selected.id); else if (selected) clearDetail();
}
function clearDetail() { selected = null; verificationOptions = { authority_bases: [], evidence: [], proof_refs: [], revision: null }; uploaded = []; $("#detail").classList.add("hidden"); $("#welcome").classList.remove("hidden"); $("#facts").replaceChildren(); $("#evidence-list").replaceChildren(); renderList(); }
async function openRecord(id) {
  const result = await api(`/admin/business-approvals/${encodeURIComponent(id)}`); selected = { ...result.data, etag: result.etag || result.data.etag };
  try { const extra = await api(`/admin/business-approvals/${encodeURIComponent(id)}/verification-options`); verificationOptions = extra.data; selected.etag = `"${extra.data.revision}"`; }
  catch (error) { verificationOptions = { authority_bases: [], evidence: [], proof_refs: selected.proof_refs || [], revision: null }; setStatus(`无法读取该记录的核查选项：${error.message}。核查已阻断。`, true); }
  $("#welcome").classList.add("hidden"); $("#detail").classList.remove("hidden"); $("#record-id").textContent = selected.id; $("#record-subject").textContent = `用户 ${selected.applicant}`;
  const [state, cls] = recordStatus(selected); $("#record-state").textContent = state; $("#record-state").className = `state-pill ${cls}`; renderFacts(selected); renderEvidence(selected); renderList();
  fillOptions($("#authority-basis"), verificationOptions.authority_bases); $("#revocation-ref").value = verificationOptions.proof_refs?.length === 1 ? verificationOptions.proof_refs[0] : "";
  $("#revocation-form").classList.toggle("hidden", selected.current === false); $("#decision").value = selected.decision === "approved" ? "verified" : "rejected"; $("#verification-reason").value = ""; updateVerifyGate();
}
function registrationPayload(form) {
  const values = new FormData(form); const applicant = String(values.get("applicant")).trim();
  const identityLabel = String(values.get("identity_label")).trim(), responsibility = String(values.get("responsibility")).trim(), basisRef = String(values.get("authority_basis_ref")).trim();
  const approverUserRef = String(values.get("approver_user_ref")).trim();
  if (!basisRef || !identityLabel || !responsibility) throw new Error("须填写外部审批人的真实身份、职责和已存在职责依据编号。");
  const from = new Date(values.get("valid_from")), until = new Date(values.get("valid_until")), approved = new Date(values.get("approved_at"));
  if (![from, until, approved].every(x => Number.isFinite(x.valueOf())) || until <= from || approved > new Date()) throw new Error("请核对实际审批时间及有效期限。");
  const documentId = String(values.get("document_id")).trim();
  return { applicant, decision: values.get("decision"), approved_at: approved.toISOString(), valid_from: from.toISOString(), valid_until: until.toISOString(),
    actual_approver: { identity_label: identityLabel, responsibility, ...(approverUserRef ? { user_ref: approverUserRef } : {}), authority_basis_ref: basisRef },
    scope: { knowledge_base_id: String(values.get("knowledge_base_id")).trim(), document_id: documentId || null, level: String(values.get("level")).trim(), category: String(values.get("category")).trim(), need_class: String(values.get("need_class")).trim() },
    operations: values.getAll("operations"), evidence_locator: String(values.get("evidence_locator")).trim(), proof_refs: [], supersedes_id: values.get("supersedes_id") || null };
}
async function start() {
  try {
    const values = new FormData($("#login-form")); const login = await api("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: values.get("username"), password: values.get("password") }) }); csrfToken = login.data.csrf_token;
    const profile = await api("/me"), capabilities = await api("/capabilities"); currentUser = profile.data.user;
    if (!currentUser.roles?.includes("system_admin") || !capabilities.data.permissions?.includes("access.manage")) throw new Error("当前账号不具备系统管理员授权管理能力。");
    $("#identity").textContent = currentUser.display_name; $("#login").classList.add("hidden"); $("#workspace").classList.remove("hidden"); await loadApprovals();
  } catch (error) { $("#login-error").textContent = error.message; csrfToken = null; }
}
$("#login-form").addEventListener("submit", event => { event.preventDefault(); start(); });
$("#reload").addEventListener("click", async () => { try { await loadApprovals(); setStatus("审批记录已刷新。"); } catch (error) { setStatus(error.message, true); } });
$("#filter").addEventListener("change", renderList); $("#decision").addEventListener("change", updateVerifyGate);
$("#new-approval").addEventListener("click", () => { const form = $("#registration-form"); form.reset(); const select = form.elements.supersedes_id; select.replaceChildren(); const none = document.createElement("option"); none.value = ""; none.textContent = "不替代"; select.append(none);
  for (const row of approvals) { const option = document.createElement("option"); option.value = row.id; option.textContent = `${row.applicant} · ${row.id}`; select.append(option); } $("#register-dialog").showModal(); });
function closeDialog() { $("#register-dialog").close(); }
$("#close-dialog").addEventListener("click", closeDialog); $("#cancel-dialog").addEventListener("click", closeDialog);
$("#registration-form").addEventListener("submit", async event => { event.preventDefault(); $("#registration-error").textContent = ""; try {
  const result = await api("/admin/business-approvals", { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": keyFor("register") }, body: JSON.stringify(registrationPayload(event.currentTarget)) });
  doneKey("register"); closeDialog(); await loadApprovals(); await openRecord(result.data.id); setStatus("事实已登记为待核查。登记操作没有批准授权。");
} catch (error) { $("#registration-error").textContent = error.message; } });
$("#evidence-form").addEventListener("submit", async event => { event.preventDefault(); if (!selected) return; const form = event.currentTarget, file = $("#evidence-file").files[0]; if (!file) return;
  const metadata = { approval_id: selected.id, source_description: $("#evidence-source").value.trim(), purpose: "external approval proof" }; const multipart = new FormData(); multipart.set("metadata", JSON.stringify(metadata)); multipart.set("file", file, file.name);
  try {
    const action = `upload:${selected.id}`; const result = await api("/admin/approval-evidence", { method: "POST", headers: { "Idempotency-Key": keyFor(action) }, body: multipart });
    uploaded.push({ ...result.data, owner_id: selected.id, source_description: metadata.source_description });
    const linkAction = `evidence-link:${selected.id}:${result.data.id}`; const updated = await api(`/admin/business-approvals/${encodeURIComponent(selected.id)}/evidence-refs`, { method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": selected.etag, "Idempotency-Key": keyFor(linkAction) }, body: JSON.stringify({ proof_refs: [...new Set([...(verificationOptions.proof_refs || []), result.data.id])] }) });
    doneKey(action); doneKey(linkAction); selected = { ...updated.data, etag: updated.data.etag }; const extra = await api(`/admin/business-approvals/${encodeURIComponent(selected.id)}/verification-options`); verificationOptions = extra.data; selected.etag = `"${extra.data.revision}"`;
    $("#evidence-message").classList.remove("bad"); $("#evidence-message").textContent = "附件已关联，但仍未核实；不能用于核查通过。"; form.reset(); renderEvidence(selected); updateVerifyGate();
  } catch (error) { $("#evidence-message").classList.add("bad"); $("#evidence-message").textContent = `附件/关联未完成：${error.message}。刷新记录可检查服务端状态。`; }
});
$("#evidence-link-form").addEventListener("submit", async event => { event.preventDefault(); if (!selected) return;
  const refs = [...$("#evidence-candidates").querySelectorAll('input[name="evidence-ref"]:checked')].map(input => input.value);
  const action = `evidence-select:${selected.id}`;
  try {
    const updated = await api(`/admin/business-approvals/${encodeURIComponent(selected.id)}/evidence-refs`, { method: "PATCH", headers: { "Content-Type": "application/json", "If-Match": selected.etag, "Idempotency-Key": keyFor(action) }, body: JSON.stringify({ proof_refs: refs }) });
    doneKey(action); selected = { ...updated.data, etag: updated.data.etag };
    const options = await api(`/admin/business-approvals/${encodeURIComponent(selected.id)}/verification-options`); verificationOptions = options.data; selected.etag = `"${options.data.revision}"`;
    renderEvidence(selected); updateVerifyGate(); setStatus("材料关联已更新；核查状态仍由服务端依据证据和职责决定。");
  } catch (error) { setStatus(`材料关联未完成：${error.message}`, true); if (error.status === 409 || error.status === 412) await openRecord(selected.id); }
});
$("#verification-form").addEventListener("submit", async event => { event.preventDefault(); if (!selected) return; updateVerifyGate(); if ($("#verify-button").disabled) return;
  const decision = $("#decision").value, basisId = $("#authority-basis").value;
  if (decision === "verified" && !verificationOptions.authority_bases.some(x => x.id === basisId)) { setStatus("缺少服务端返回的有效、匹配职责依据。", true); return; }
  const action = `verify:${selected.id}`;
  try { const result = await api(`/admin/business-approvals/${encodeURIComponent(selected.id)}/verification`, { method: "POST", headers: { "Content-Type": "application/json", "If-Match": selected.etag, "Idempotency-Key": keyFor(action) }, body: JSON.stringify({ decision, reason: $("#verification-reason").value.trim(), checked_evidence_refs: verificationOptions.proof_refs || [], authority_basis_ref: basisId }) });
    doneKey(action); await loadApprovals(); await openRecord(result.data.id); setStatus(result.data.verification_state === "verified" ? "服务端核查结果：已核实。" : "服务端核查结果：未通过。", result.data.verification_state !== "verified");
  } catch (error) { setStatus(`服务端未接受核查记录：${error.message}`, true); if (error.status === 409) await openRecord(selected.id); }
});
$("#revocation-form").addEventListener("submit", async event => { event.preventDefault(); if (!selected) return; const action = `revoke:${selected.id}`;
  try { const result = await api(`/admin/business-approvals/${encodeURIComponent(selected.id)}/revocations`, { method: "POST", headers: { "Content-Type": "application/json", "If-Match": selected.etag, "Idempotency-Key": keyFor(action) }, body: JSON.stringify({ verified_revocation_evidence_ref: $("#revocation-ref").value.trim(), reason: $("#revocation-reason").value.trim() }) });
    doneKey(action); $("#revocation-result").textContent = `撤销登记回执：${result.data.id}。请刷新核对授权当前状态。`; await loadApprovals(); await openRecord(selected.id);
  } catch (error) { $("#revocation-result").textContent = `服务端未接受撤销登记：${error.message}`; }
});
$("#logout").addEventListener("click", async () => { try { await api("/auth/logout", { method: "POST" }); } catch {} clearForLostAccess(); $("#login-error").textContent = ""; });
