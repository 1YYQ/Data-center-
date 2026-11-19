import { useEffect, useState } from "react";
import { api, etag, id } from "../../api";
import { stateLabel } from "../../labels";
import { IndexManagement } from "./IndexManagement";
import { processingLabel } from "./processingLabels";
import type { Preview, ReviewSet, ReviewSets, Version } from "./types";

type Receipt = { operation_receipt_ref: string; index_readiness: string };
type Action = "publish" | "withdraw" | "source_rebind";
const actions: Record<Action, string> = { publish: "发布", withdraw: "撤回", source_rebind: "更换现行来源" };

function reviewDate(row: ReviewSet) {
  return row.reviewed_at ? new Date(row.reviewed_at).toLocaleString("zh-CN", { hour12: false }) : "时间未知";
}

export function Publication({ base, kb, version, invalidation, visible, onChange, perform }: {
  base: string; kb: string; version: Version; invalidation: number; visible: boolean;
  onChange: () => Promise<void>; perform: (action: () => Promise<void>, success?: string) => Promise<void>;
}) {
  const [reference, setReference] = useState(version.reference_access_state === "open");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [reason, setReason] = useState("");
  const [receipt, setReceipt] = useState("");
  const [reviews, setReviews] = useState<ReviewSets | null>(null);
  const [loadingReviews, setLoadingReviews] = useState(false);
  const [candidate, setCandidate] = useState("");
  const [mode, setMode] = useState<"refresh" | "quality_repair">("refresh");
  const selectedReviewId = version.selected_review_set_id;
  const candidateRows = reviews?.items.filter(row => row.is_ir && !row.selected && row.id !== selectedReviewId) ?? [];
  const selected = candidateRows.find(row => row.id === candidate);
  const candidatesManageable = ["draft", "effective"].includes(version.publish_status);

  useEffect(() => {
    let active = true;
    setPreview(null); setReference(version.reference_access_state === "open"); setReviews(null); setLoadingReviews(true);
    void api.get<ReviewSets>(base + "/review-sets").then(result => { if (active) setReviews(result); }).catch(() => {})
      .finally(() => { if (active) setLoadingReviews(false); });
    return () => { active = false; };
  }, [base, invalidation, version.reference_access_state]);

  async function showPreview(action: Action) {
    setPreview(null);
    const payload = action === "source_rebind" && selected
      ? { action, mode, chunk_set_id: selected.chunk_set_id, review_set_id: selected.id } : { action };
    setPreview(await api.mutate<Preview>(base + "/publication-preview", payload));
  }
  async function finishPreview(heldPreview: Preview) {
    const action = heldPreview.action as Action;
    if (action === "withdraw" && !reason.trim()) throw Error("请填写撤回原因。");
    const result = await api.mutate<Receipt | { operation: Receipt }>(base + "/" + action.replaceAll("_", "-"),
      { preview_id: heldPreview.preview_id, ...(action === "withdraw" ? { reason: reason.trim() } : {}) }, heldPreview.document_etag);
    const operation = "operation" in result ? result.operation : result;
    const suffix = operation.index_readiness ? " · 操作时索引 " + stateLabel(operation.index_readiness) : "";
    setReceipt(actions[action] + "回执：" + operation.operation_receipt_ref + suffix);
    setPreview(null);
    await onChange();
  }

  const previewCard = preview && <div className="preview preview-card">
      <h3>{preview.allowed ? "预检允许" + actions[preview.action as Action] : "仍有阻断项"}</h3>
      {preview.blocked_reasons.length > 0 && <ul>{preview.blocked_reasons.map((value, index) =>
        <li key={index}>{processingLabel(value, stateLabel(value))}</li>)}</ul>}
      <p>预检有效至 {preview.expires_at}，目标版本 {version.revision_label || version.version_no}</p>
      <details>
        <summary>查看影响范围</summary>
        {preview.action === "source_rebind" ? <RebindImpact impact={preview.impact} /> : <>
          <p>此前现行版本 {Array.isArray(preview.impact.previous_current_ids) ? preview.impact.previous_current_ids.join("、") || "无" : "未知"}</p>
          <p>涉及查询回答、计算与报告。</p>
        </>}
        <p>索引 {processingLabel(String(preview.impact.index_readiness), stateLabel(String(preview.impact.index_readiness || "待核对")))}</p>
      </details>
      {preview.allowed && <button onClick={() => void perform(() => finishPreview(preview), "已读取操作回执，请核对版本与索引状态。")}>
        按此预检{actions[preview.action as Action]}
      </button>}
  </div>;

  return <>
    <p className="status">原文参考状态 {version.reference_access_state === "open" ? "已开放" : "未开放"}</p>
    <label className="toggle"><input type="checkbox" checked={reference} onChange={event => setReference(event.target.checked)} />仅供参考开放此精确版本原文</label>
    <button className="ghost" onClick={() => void perform(async () => {
      await api.mutate(base + "/reference-access", { state: reference ? "open" : "closed", reason: "管理员显式设置精确版本原文参考" }, etag(version), "PUT");
      setPreview(null);
      await onChange();
    }, "已保存原文参考状态。")}>保存参考状态</button>

    <section className="pdf-review-section" aria-labelledby="pdf-source-selection">
      <h3 id="pdf-source-selection">已选来源与核验候选</h3>
      <p className="locator">{selectedReviewId
        ? `已选用核验集 ${selectedReviewId.slice(0, 8)} · 切分集合 ${version.selected_chunk_set_id?.slice(0, 8) || "待核对"}`
        : "此版本尚未选用切分来源。"}</p>
      {loadingReviews && <p role="status" className="muted">正在读取核验集…</p>}
      {!loadingReviews && !reviews && <p role="status" className="muted">当前无法读取核验集，请刷新管理范围重试。</p>}
      {reviews && candidateRows.length === 0 && <p className="muted">当前没有待选核验候选。</p>}
      {candidateRows.length > 0 && <label>待选核验候选<select value={selected?.id || ""} onChange={event => {
        setCandidate(event.target.value); setPreview(null);
      }}>
        <option value="">请选择候选</option>
        {candidateRows.map(row => <option key={row.id} value={row.id}>
          {reviewDate(row)} · {row.coverage_state === "complete" ? "核验当前有效" : "核验已失效"} · {row.id.slice(0, 8)}
        </option>)}
      </select></label>}
      {reviews?.next_cursor && <button className="ghost space" onClick={() => void perform(async () => {
        const next = await api.get<ReviewSets>(base + "/review-sets?cursor=" + id(reviews.next_cursor!));
        setReviews({ ...next, items: [...reviews.items, ...next.items] });
      })}>读取更多核验集</button>}
      {selected && <p className="locator">候选解析批次 {selected.parse_run_id.slice(0, 8)} · 切分集合 {selected.chunk_set_id.slice(0, 8)}</p>}
      {selected && candidatesManageable && (version.publish_status === "draft"
        ? <button className="space" disabled={selected.coverage_state !== "complete"} onClick={() => void perform(async () => {
          await api.mutate(base + "/source-selection", { chunk_set_id: selected.chunk_set_id, review_set_id: selected.id }, etag(version));
          setCandidate("");
          await onChange();
        }, "草稿来源已选用，可继续检查发布条件。")}>选用此草稿来源</button>
        : <>
          <label>更换原因类型<select value={mode} onChange={event => {
            setMode(event.target.value === "quality_repair" ? "quality_repair" : "refresh"); setPreview(null);
          }}>
            <option value="refresh">更新现行批次</option>
            <option value="quality_repair">修复现行来源的质量问题</option>
          </select></label>
          <button className="space" disabled={selected.coverage_state !== "complete"} onClick={() => void perform(
            () => showPreview("source_rebind"), "已读取同版来源变更的预检结果。"
          )}>检查同版更换来源</button>
        </>)}
      {preview?.action === "source_rebind" && previewCard}
    </section>

    <div className="rule" />
    <button onClick={() => void perform(() => showPreview("publish"), "预检回执已保存。")}>检查发布条件</button>
    {preview?.action !== "source_rebind" && previewCard}
    <p className="status" role="status">{receipt}</p>
    <IndexManagement key={kb} kb={kb} versionId={version.id} invalidation={invalidation} visible={visible} perform={perform} />
    <div className="rule" />
    <label>撤回原因<input value={reason} maxLength={500} onChange={event => setReason(event.target.value)} /></label>
    <button className="ghost space" onClick={() => void perform(() => showPreview("withdraw"), "撤回预检回执已保存。")}>检查撤回影响</button>
  </>;
}

function RebindImpact({ impact }: { impact: Preview["impact"] }) {
  const refs = Array.isArray(impact.quality_hold_refs) ? impact.quality_hold_refs.filter((value): value is string => typeof value === "string") : [];
  const repair = impact.repair_basis && typeof impact.repair_basis === "object" ? impact.repair_basis as Record<string, unknown> : {};
  return <>
    <p>更换方式 {impact.mode === "quality_repair" ? "修复来源质量问题" : "更新现行批次"}</p>
    {typeof impact.old_chunk_set_id === "string" && typeof impact.chunk_set_id === "string" && <p>
      切分集合从 {impact.old_chunk_set_id.slice(0, 8)} 更换为 {impact.chunk_set_id.slice(0, 8)}。
    </p>}
    {impact.mode === "quality_repair" && <>
      <p>预检涉及 {refs.length} 项暂停记录。</p>
      {!!refs.length && <ul>{refs.map(ref => {
        const info = repair[ref] as { revision?: number; affected_refs?: string[] } | undefined;
        return <li key={ref}>
          暂停记录 {ref}{typeof info?.revision === "number" && ` · 修订 ${info.revision}`}
          {!!info?.affected_refs?.length && <p>受影响的来源 {info.affected_refs.join("、")}</p>}
        </li>;
      })}</ul>}
    </>}
    <p>更换后需要重新构建索引。历史回答仍可沿原有引用核对。</p>
  </>;
}
