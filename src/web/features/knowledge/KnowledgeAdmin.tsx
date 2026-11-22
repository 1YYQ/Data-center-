import { useEffect, useState } from "react";
import { api, id } from "../../api";
import { Field, formValues, PendingOperations, Status, text, useOperation, Workspace } from "../../components";
import { ConfirmationForm, ConfirmationRecord, EvidenceUpload } from "./confirmations";
import { Publication } from "./publication";
import { KnowledgeBaseCreation, DocumentCreation } from "./creation";
import { LifecyclePanel } from "./lifecycle";
import { SourceForm } from "./source";
import type { SourceView } from "./source";
import type { ChunkSet, Confirmation, ConfirmationOptions, ParseBlocks, ParsePages, ParseRun, Version } from "./types";
import { versionName } from "./types";
import { ParseReview, readParsePages } from "./ParseReview";
import { FeedbackInbox } from "../feedback";
import { useSession } from "../../session";
import { createProjectionLease } from "../../projectionLease";

type Item = { id: string; name: string };
type Document = { id: string; title: string };

export function KnowledgeAdmin() { return <Workspace title="资料库与核验" eyebrow="KNOWLEDGE GOVERNANCE" intro="核对真实依据与原文批次，再查看发布条件和回执。" role="knowledge_manager"><KnowledgeHome /></Workspace>; }
function KnowledgeHome() {
  const { profile } = useSession();
  const feedback = new URLSearchParams(location.search).has("feedback");
  if (!profile?.capabilities.includes("feedback.handle")) return <KnowledgeWorkspace />;
  return <div className="stack">
    <nav aria-label="资料治理入口" className="tabs">
      <a className="ghost" href="/workspace/manage" aria-current={feedback ? undefined : "page"}>资料维护</a>
      <a className="ghost" href="/workspace/manage?feedback=1" aria-current={feedback ? "page" : undefined}>资料反馈</a>
    </nav>
    {feedback ? <FeedbackInbox /> : <KnowledgeWorkspace />}
  </div>;
}
function KnowledgeWorkspace() {
  const op = useOperation();
  const [kbs, setKbs] = useState<Item[]>([]), [kb, setKb] = useState("");
  const [docs, setDocs] = useState<Document[]>([]), [doc, setDoc] = useState("");
  const [versions, setVersions] = useState<Version[]>([]), [version, setVersion] = useState<Version | null>(null);
  const [runs, setRuns] = useState<ParseRun[]>([]), [run, setRun] = useState("");
  const [currentRun, setCurrentRun] = useState<ParseRun | null>(null);
  const [sets, setSets] = useState<ChunkSet[]>([]), [chunk, setChunk] = useState("");
  const [blocks, setBlocks] = useState<ParseBlocks | null>(null), [pages, setPages] = useState<ParsePages | null>(null);
  const [confirmations, setConfirmations] = useState<Confirmation[]>([]), [options, setOptions] = useState<ConfirmationOptions | null>(null);
  const [tab, setTab] = useState("review"), [publicationEpoch, setPublicationEpoch] = useState(0);
  const [confirmationJump, setConfirmationJump] = useState(0);
  const base = version ? `/documents/${id(doc)}/versions/${id(version.id)}` : "";
  const [source, setSource] = useState<SourceView | null>(null);
  const supersededConfirmations = new Set(confirmations.map(row => row.supersedes_confirmation_id));

  function clearRun() {
    setCurrentRun(null); setBlocks(null); setPages(null); setSets([]); setChunk("");
  }

  async function loadRun(runId: string, versionSelection = version?.selected_chunk_set_id, previousChunk = run === runId ? chunk : "") {
    setRun(runId);
    if (!runId || runId !== run) clearRun();
    if (!runId) return;
    const prefix = `/parse-runs/${id(runId)}`;
    try {
      const [current, response, pageView, chunks] = await Promise.all([
        api.get<ParseRun>(prefix), api.get<ParseBlocks>(prefix + "/blocks"), readParsePages(runId),
        api.get<{ items: ChunkSet[] }>(prefix + "/chunk-sets"),
      ]);
      setCurrentRun(current); setBlocks(response); setPages(pageView); setSets(chunks.items);
      setRuns(rows => rows.map(row => row.id === current.id ? current : row));
      setChunk(chunks.items.find(row => row.id === previousChunk)?.id
        || chunks.items.find(row => row.id === versionSelection)?.id
        || chunks.items.find(row => row.status === "ready")?.id || chunks.items[0]?.id || "");
    } catch (error) {
      clearRun();
      throw error;
    }
  }
  async function selectVersion(documentId: string, selected: Version | null) {
    setVersion(selected); setRuns([]); setRun(""); clearRun(); setConfirmations([]); setOptions(null); setSource(null); setPublicationEpoch(value => value + 1);
    if (!selected) return;
    const prefix = `/documents/${id(documentId)}/versions/${id(selected.id)}`;
    const [parsed, confirms, opts, sourceView] = await Promise.all([
      api.get<{ items: ParseRun[] }>(prefix + "/parse-runs"),
      api.get<{ items: Confirmation[] }>(prefix + "/business-confirmations"),
      api.get<ConfirmationOptions>(prefix + "/confirmation-options"),
      api.get<SourceView>(prefix + "/source-metadata"),
    ]);
    setRuns(parsed.items); setConfirmations(confirms.items); setOptions(opts); setSource(sourceView);
    await loadRun(parsed.items[0]?.id || "", selected.selected_chunk_set_id, "");
  }
  async function loadVersions(documentId: string, preferred = "") {
    setDoc(documentId); setVersions([]); setVersion(null);
    if (!documentId) { await selectVersion("", null); return; }
    const response = await api.get<{ items: Version[] }>(`/documents/${id(documentId)}/versions`); setVersions(response.items);
    await selectVersion(documentId, response.items.find(row => row.id === (preferred || version?.id)) || response.items.find(row => row.publish_status === "effective") || response.items[0] || null);
  }
  async function loadDocuments(kbId: string, preferred = "", preferredVersion = "") {
    setKb(kbId); setDocs([]); setDoc(""); setVersion(null); setVersions([]); setSource(null); setOptions(null); setRun(""); setRuns([]); clearRun(); setConfirmations([]);
    if (!kbId) return;
    const response = await api.get<{ items: Document[] }>(`/knowledge-bases/${id(kbId)}/documents`); setDocs(response.items);
    await loadVersions(response.items.find(row => row.id === preferred)?.id || response.items[0]?.id || "", preferredVersion);
  }
  async function refresh(preferred = run) {
    const [response, confirmResult, opts, sourceView, parsed] = await Promise.all([
      api.get<{ items: Version[] }>(`/documents/${id(doc)}/versions`),
      api.get<{ items: Confirmation[] }>(base + "/business-confirmations"),
      api.get<ConfirmationOptions>(base + "/confirmation-options"),
      api.get<SourceView>(base + "/source-metadata"),
      api.get<{ items: ParseRun[] }>(base + "/parse-runs"),
    ]);
    const selected = response.items.find(row => row.id === version?.id);
    if (!selected) {
      await selectVersion(doc, null);
      throw Error("所选版本已不可访问，请刷新管理范围。");
    }
    setVersions(response.items); setVersion(selected); setPublicationEpoch(value => value + 1);
    setConfirmations(confirmResult.items); setOptions(opts); setSource(sourceView); setRuns(parsed.items);
    await loadRun(parsed.items.find(row => row.id === preferred)?.id || parsed.items[0]?.id || "", selected.selected_chunk_set_id);
  }
  useEffect(() => { void op.perform(async () => { const response = await api.get<{ items: Item[] }>("/knowledge-bases"); setKbs(response.items); await loadDocuments(response.items[0]?.id || ""); }); }, []);
  useEffect(() => {
    if (confirmationJump) document.getElementById("knowledge-confirmation-kind")?.focus();
  }, [confirmationJump]);
  useEffect(() => {
    if (!base) return;
    let active = true, running = false;
    const lease = createProjectionLease(() => {
      if (!active) return;
      setVersion(null); clearRun(); setConfirmations([]); setOptions(null); setSource(null); setDocs([]); setVersions([]);
      op.say("资料管理范围待重新核对，已隐藏内容。请刷新管理范围，未提交更改不会自动保存。");
    });
    async function verify() {
      if (!active || running || !navigator.onLine || document.hidden) return;
      const started = performance.now(); running = true;
      try { await api.get(base + "/confirmation-options"); if (active) lease.renew(started); }
      catch { if (active) lease.invalidate(); }
      finally { running = false; }
    }
    lease.renew(performance.now()); const timer = setInterval(() => void verify(), 10000);
    window.addEventListener("focus", verify); document.addEventListener("visibilitychange", verify);
    return () => { active = false; lease.dispose(); clearInterval(timer); window.removeEventListener("focus", verify); document.removeEventListener("visibilitychange", verify); };
  }, [base]);
  return <fieldset className="operation-fields" disabled={op.busy} aria-busy={op.busy}><div className="manage-grid"><aside className="panel knowledge-selection" id="collect"><div className="section-heading"><div><p className="eyebrow">WORK QUEUE</p><h2>选择要维护的资料</h2></div><button className="ghost" onClick={() => void op.perform(() => loadDocuments(kb, doc))}>刷新</button></div><KnowledgeBaseCreation perform={op.perform} /><DocumentCreation key={kb} kb={kb} perform={op.perform} onCreated={async (documentId, versionId) => { await loadDocuments(kb, documentId, versionId); op.say("新文档草稿已保存：" + documentId + " / " + versionId + "；分类和业务确认仍待核对。"); }} /><label className="space">知识库<select value={kb} onChange={event => void op.perform(() => loadDocuments(event.target.value))}>{kbs.map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label><label className="space">文档<select value={doc} onChange={event => void op.perform(() => loadVersions(event.target.value))}>{docs.map(row => <option key={row.id} value={row.id}>{row.title}</option>)}</select></label><label className="space">版本<select value={version?.id || ""} onChange={event => void op.perform(() => selectVersion(doc, versions.find(row => row.id === event.target.value) || null))}>{versions.map(row => <option key={row.id} value={row.id}>{versionName(row)}</option>)}</select></label><p className="status muted">{version ? `${versionName(version)} · 原文参考 ${version.reference_access_state === "open" ? "已开放" : "未开放"}` : "当前没有可管理的资料版本。"}</p>{doc && <details><summary>上传此文档新版本 PDF</summary><p className="muted">上传保存为草稿；现行发布版与确认资格由服务端保留。只使用获准本地处理的文件。</p><form className="stack" onSubmit={event => { event.preventDefault(); const values = formValues(event), form = event.currentTarget; void op.perform(async () => { const file = values.get("file"); if (!(file instanceof File) || !file.size) throw Error("请选择新版本 PDF。"); const result = await api.uploadVersion<{ version: Version }>(doc, file, { revision_label: text(values, "revision_label") || null, model_scope: text(values, "model_scope") || null }); form.reset(); form.closest("details")?.removeAttribute("open"); await loadVersions(doc, result.version.id); op.say("新版本草稿已保存：" + result.version.id + "；请选择本地解析继续核验。"); }); }}><Field label="版本标识（可选）" name="revision_label" required={false} /><Field label="适用型号（逗号分隔，可选）" name="model_scope" required={false} /><label>新版本 PDF<input name="file" type="file" accept="application/pdf,.pdf" required /></label><button>上传新版本</button></form></details>}</aside>
    <section className="panel detail">{version && <div className="version-states" aria-label="版本业务状态"><span>适用性 <strong>{version.applicability_status === "confirmed" ? "已确认" : "待确认"}</strong></span><span>内容核验 <strong>{version.content_review_status === "approved" ? "已核验" : "待核验"}</strong></span><span>发布状态 <strong>{version.publish_status === "effective" ? "当前发布版" : version.publish_status === "draft" ? "未发布" : "历史版本"}</strong></span></div>}<PendingOperations perform={op.perform} onRecovered={() => loadDocuments(kb, doc)} /><nav className="tabs" aria-label="资料维护步骤">{[["review", "原文核验"], ["confirm", "业务依据"], ["publication", "发布检查"], ["lifecycle", "生命周期"]].map(([key, label]) => <button key={key} className={tab === key ? "active" : ""} aria-current={tab === key ? "page" : undefined} onClick={() => setTab(key)}>{label}</button>)}</nav>{tab === "lifecycle" && kb && <LifecyclePanel key={kb} kb={kb} perform={op.perform} onChanged={async () => { if (version) await refresh(); }} onDeleted={async documentId => { if (doc === documentId) { setDoc(""); setVersion(null); setVersions([]); setRuns([]); setRun(""); clearRun(); setConfirmations([]); setOptions(null); setSource(null); } setDocs(current => current.filter(row => row.id !== documentId)); }} />}{version && <>
      <div hidden={tab !== "review"}>
        <ParseReview key={`${version.id}-${run}`} base={base} version={version} runs={runs} run={run} current={currentRun}
          blocks={blocks} pages={pages} sets={sets} chunk={chunk} confirmations={confirmations} perform={op.perform}
          selectedRun={loadRun} selectedChunk={setChunk} onChange={refresh} onRequested={refresh} say={op.say}
          openConfirm={() => { setTab("confirm"); setConfirmationJump(value => value + 1); }} openPublication={() => setTab("publication")} />
      </div>
      <section hidden={tab !== "confirm"}>{source && <SourceForm key={`${version.id}-${source.etag}`} source={source} base={base} draft={version.publish_status === "draft"} perform={op.perform} onChange={refresh} />}{options && <ConfirmationForm key={`${version.id}-${version.revision}-${run}-${confirmationJump}`} options={options} base={base} run={run} blocks={blocks} preferKeyContent={confirmationJump > 0} onChange={refresh} perform={op.perform} />}<EvidenceUpload base={base} perform={op.perform} onChange={refresh} /><h3>确认记录与核查</h3><div className="records">{confirmations.map(row => <ConfirmationRecord key={`${row.id}-${row.etag}`} row={row} base={base} perform={op.perform} onChange={refresh}
        currentOptions={options} superseded={supersededConfirmations.has(row.id)} />)}{!confirmations.length && <p className="muted">本版本尚无业务确认登记。</p>}</div></section>
      <section hidden={tab !== "publication"}><Publication key={version.id} invalidation={publicationEpoch} visible={tab === "publication"} base={base} kb={kb} version={version} onChange={refresh} perform={op.perform} /></section>
    </>}<Status message={op.message} bad={op.bad} /></section></div></fieldset>;
}
