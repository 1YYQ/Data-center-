import { useState } from "react";
import { api, etag, id } from "../../api";
import { formValues, selectedValues, text } from "../../components";
import { stateLabel } from "../../labels";
import { processingLabel } from "./processingLabels";
import type { ChunkSet, Confirmation, ParseBlocks, Version } from "./types";

type Perform = (action: () => Promise<void>, success?: string) => Promise<void>;
const splitter = "dc.semantic-bge-m3/1";

export function CollectionReview({ base, version, run, blocks, sets, chunk, confirmations, perform, selectedChunk, onChange, say, openPublication }: {
  base: string; version: Version; run: string; blocks: ParseBlocks; sets: ChunkSet[]; chunk: string; confirmations: Confirmation[];
  perform: Perform; selectedChunk: (chunk: string) => void; onChange: () => Promise<void>; say: (message: string) => void; openPublication: () => void;
}) {
  const [professionals, setProfessionals] = useState<string[]>([]);
  const selected = sets.find(row => row.id === chunk);
  const modern = Boolean(blocks.ir_schema_version);
  const canManage = ["draft", "effective"].includes(version.publish_status);
  const isSelected = selected && (selected.selected || selected.id === version.selected_chunk_set_id);
  const canRequest = canManage && Boolean(blocks.milestones?.structured && blocks.source_qc && blocks.source_qc.status !== "not_run");
  const choices = confirmations.filter(row => row.current && row.kind === "key_content" && row.conclusion === "accepted" && row.scope.parse_run_id === run);
  const matchingProfessionals = professionals.filter(ref => choices.some(row => row.id === ref));
  const canSave = selected?.status === "ready" && (modern
    ? canManage && !isSelected && selected.quality_current === true && blocks.source_verification?.state === "verified"
    : version.publish_status === "draft");
  const quality = selected?.quality_status === "passed" && selected.quality_current === false ? "stale" : selected?.quality_status;
  const hasReview = Boolean(selected?.review_set_ids?.length);
  return <section className="pdf-review-section" aria-labelledby="pdf-collection-title">
    <h3 id="pdf-collection-title">切分集合与完整核验</h3>
    {modern && canManage && <div className="section-actions">
      <button className="ghost" disabled={!canRequest} onClick={() => void perform(async () => {
        const result = await api.mutate<ChunkSet>(`/parse-runs/${id(run)}/chunk-sets`, { splitter_profile_revision_id: splitter }, etag(version));
        await onChange();
        if (result.id) selectedChunk(result.id);
        say("已收到切分请求。刷新解析批次可读取集合状态。");
      })}>请求生成切分集合</button>
    </div>}
    {modern && canManage && !canRequest && <p className="muted">结构和源技术检查报告就绪后可请求切分。</p>}
    {sets.length > 0 ? <label>切分集合<select value={chunk} onChange={event => selectedChunk(event.target.value)}>
      {!chunk && <option value="">请选择切分集合</option>}
      {sets.map(row => <option key={row.id} value={row.id}>
        {row.splitter_version === splitter ? "语义切分" : row.splitter_version} · {stateLabel(row.status)}{(row.selected || row.id === version.selected_chunk_set_id) ? " · 已选用" : ""} · {row.id.slice(0, 8)}
      </option>)}
    </select></label> : <p className="muted">当前批次暂无切分集合。</p>}
    {selected && <div className="pdf-run-summary">
      <p><strong>集合{stateLabel(selected.status)}</strong> · {isSelected ? "此版本已选用" : "此版本尚未选用"}</p>
      {modern && <>
        <p>集合质量检查 {processingLabel(quality, "等待结果")}{selected.status === "ready" && ` · ${selected.child_count ?? 0} 个检索片段、${selected.parent_count ?? 0} 个上级片段`}</p>
        <p>完整核验集 {hasReview ? "已有记录，请在发布检查查看当前有效性" : "尚未创建"}</p>
        {selected.error_code && <p className="warning">{processingLabel(selected.error_code, "集合构建遇到问题，请刷新查看后台处理结果。")}</p>}
        {!!selected.quality_issues?.length && <ul className="pdf-review-reasons">
          {[...new Set(selected.quality_issues.map(issue => issue.code))].map(code =>
            <li key={code}>{processingLabel(code, "切分内容或原文覆盖有待核对。")}</li>)}
        </ul>}
      </>}
    </div>}
    {hasReview && modern && <button className="ghost space" onClick={openPublication}>查看发布检查与选用状态</button>}
    {selected?.status === "ready" && (!modern || (canManage && !isSelected)) && <form onSubmit={event => {
      event.preventDefault();
      const values = formValues(event);
      void perform(async () => {
        if (!canSave) return;
        const current = await api.get<ParseBlocks>(`/parse-runs/${id(run)}/blocks`);
        const members = modern ? current.items.filter(block => block.structure_role === "leaf") : current.items;
        const reason = modern ? text(values, "candidate_reason") : "由管理界面逐块核验后提交";
        if (!reason) throw Error("请填写本次核验登记说明。");
        const result = await api.mutate<{ id: string }>(base + "/review-sets", { parse_run_id: run, chunk_set_id: chunk,
          reviewed_block_refs: members.map(row => ({ id: row.id, revision: row.revision, content_hash: row.content_hash })),
          professional_confirmation_refs: matchingProfessionals, reason }, etag(version));
        await onChange();
        say(modern ? "完整核验候选已登记，请在发布检查确认当前状态。" : "完整核验集已创建：" + result.id);
      });
    }}>
      <label className="space">匹配本批次且当前有效的专业确认<select multiple size={3} value={matchingProfessionals} onChange={event => setProfessionals(selectedValues(event.target))}>
        {choices.map(row => <option key={row.id} value={row.id}>{row.actual_confirmer?.identity_label || "未知确认人"} · {row.confirmed_at || "时间未知"}</option>)}
      </select></label>
      <p className="muted">请选择覆盖关键内容、原页定位和适用关系的真实专业确认。</p>
      {modern && <label>本次核验登记说明<input name="candidate_reason" required maxLength={500} disabled={!canSave} /></label>}
      <button className="space" disabled={!canSave}>{modern ? "创建完整核验候选" : "创建完整核验集"}</button>
      {modern && !canSave && <p className="muted">源人工核验和集合质量检查都有效后才能登记候选。</p>}
    </form>}
  </section>;
}
