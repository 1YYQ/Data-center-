import { useEffect, useState } from "react";
import { api, etag, id } from "../../api";
import { formValues, text } from "../../components";
import { stateLabel } from "../../labels";
import { BlockReview, blockName, OriginalLink } from "./BlockReview";
import { CollectionReview } from "./CollectionReview";
import { processingLabel } from "./processingLabels";
import { blockPage } from "./types";
import type { Block, BlockRelation, ChunkSet, Confirmation, ParseBlocks, ParsePage, ParsePages, ParseRun, SourceIssue, Version } from "./types";

type Perform = (action: () => Promise<void>, success?: string) => Promise<void>;
const cancellable = new Set(["pending", "running", "waiting_external", "reconciliation_required"]);
const pageLimit = 50;
const sliceKeys = ["rows", "source_parts", "formula_symbols", "latex_raw", "mathml_raw", "asset_parts"];

export function runName(run: ParseRun) {
  const date = run.created_at ? new Date(run.created_at) : null;
  const time = date && Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "";
  return [processingLabel(run.status, stateLabel(run.status)), time, run.id.slice(0, 8)].filter(Boolean).join(" · ");
}

export function readParsePages(run: string, after = 0, limit = pageLimit) {
  return api.get<ParsePages>(`/parse-runs/${id(run)}/pages?limit=${limit}&after=${after}`);
}

export function ParseReview({ base, version, runs, run, current, blocks, pages, sets, chunk, confirmations,
  perform, selectedRun, selectedChunk, onChange, onRequested, say, openConfirm, openPublication }: {
  base: string; version: Version; runs: ParseRun[]; run: string; current: ParseRun | null;
  blocks: ParseBlocks | null; pages: ParsePages | null; sets: ChunkSet[]; chunk: string; confirmations: Confirmation[];
  perform: Perform; selectedRun: (run: string) => Promise<void>; selectedChunk: (chunk: string) => void;
  onChange: () => Promise<void>; onRequested: (run: string) => Promise<void>; say: (message: string) => void; openConfirm: () => void; openPublication: () => void;
}) {
  const exactSelection = sets.some(row => row.selected || row.id === version.selected_chunk_set_id);
  const canManageSource = version.publish_status === "draft" || version.publish_status === "effective";
  const wasPublished = Boolean(current?.was_published || blocks?.was_published || (version.publish_status === "effective" && exactSelection));
  const sourceReadOnly = !canManageSource || wasPublished;
  const currentState = current && processingLabel(current.status, stateLabel(current.status));
  const total = pages?.total_page_count ?? current?.total_page_count;
  return <section aria-labelledby="pdf-review-title" className="pdf-review">
    <h2 id="pdf-review-title">解析批次与原页核验</h2>
    <p className="muted">请求由后台任务处理。刷新可读取最新进度、原页和核验结果。</p>
    <div className="section-actions">
      <button disabled={!canManageSource} onClick={() => void perform(async () => {
        const result = await api.mutate<{ parse_run_id: string; status: string }>(base + "/parse-runs", {});
        await onRequested(result.parse_run_id);
        say("已收到本地解析请求，请查看批次状态。");
      })}>请求本地解析此版本</button>
      <button className="ghost" onClick={() => void perform(() => onRequested(run), "已读取最新批次状态。")}>刷新解析批次</button>
    </div>
    <label>解析批次<select value={run} disabled={!runs.length} onChange={event => void perform(() => selectedRun(event.target.value))}>
      {!runs.length && <option value="">尚无解析批次</option>}
      {runs.map(row => <option key={row.id} value={row.id}>{runName(row)}</option>)}
    </select></label>
    <p className="pdf-original-link"><OriginalLink base={base} /></p>
    {!run && <p className="muted">此版本尚无解析批次。原件可先单独查阅。</p>}
    {run && !current && <p role="status" className="muted">尚未读取此批次的状态，请刷新重试。</p>}
    {current && <div className="pdf-run-summary">
      <p role="status"><strong>批次状态 {currentState}</strong>{current.processing_stage && ` · ${processingLabel(current.processing_stage, "后台处理")}`}
        {typeof total === "number" && ` · 已记录处理 ${current.processed_page_count ?? 0} / ${total} 页`}</p>
      {current.error_code && <p className="warning">{processingLabel(current.error_code, "当前阶段遇到问题，请刷新批次查看进度。")}</p>}
      {current.status === "reconciliation_required" && <p className="warning">后台须核对原任务结果。可刷新读取当前状态。</p>}
      {current.stage_state && !current.error_code && current.stage_state !== current.status && <p className="locator">{processingLabel(current.stage_state, "阶段详情待更新")}</p>}
      {sourceReadOnly && <p className="locator">{wasPublished ? "此批次曾用于正式发布，原文核验记录保留为只读。" : "历史版本的原文核验记录为只读。"}</p>}
      {canManageSource && ["local", "provider"].includes(current.origin || "") && cancellable.has(current.status) && <details>
        <summary>取消此解析任务</summary>
        <form className="stack" onSubmit={event => {
          event.preventDefault();
          const values = formValues(event);
          const form = event.currentTarget;
          void perform(async () => {
            const reason = text(values, "cancel_reason");
            if (!reason) throw Error("请填写取消原因。");
            await api.mutate(`/parse-runs/${id(run)}/cancel`, { reason }, etag(version));
            form.reset();
            await onChange();
          }, "已读取取消回执，请核对批次状态。");
        }}>
          <label>取消原因<input name="cancel_reason" required maxLength={500} /></label>
          <div><button className="ghost">确认取消解析</button></div>
        </form>
      </details>}
    </div>}
    {blocks && current && <EvidenceReview key={run} base={base} run={run} current={current} blocks={blocks} initialPages={pages}
      immutable={sourceReadOnly || (Boolean(blocks.ir_schema_version) && !blocks.milestones?.structured)}
      perform={perform} onChange={onChange} openConfirm={openConfirm} />}
    {run && blocks && <CollectionReview base={base} version={version} run={run} blocks={blocks} sets={sets} chunk={chunk}
      confirmations={confirmations} perform={perform} selectedChunk={selectedChunk} onChange={onChange} say={say} openPublication={openPublication} />}
  </section>;
}

function EvidenceReview({ run, base, blocks, current, initialPages, immutable, perform, onChange, openConfirm }: {
  run: string; base: string; blocks: ParseBlocks; current: ParseRun; initialPages: ParsePages | null; immutable: boolean;
  perform: Perform; onChange: () => Promise<void>; openConfirm: () => void;
}) {
  const [extraPages, setExtraPages] = useState<ParsePages | null>(null);
  const [jumpedPage, setJumpedPage] = useState<ParsePage>();
  const [selection, setSelection] = useState<number | "structure" | null>(null);
  const [focusTarget, setFocusTarget] = useState<{ elementId: string } | null>(null);
  const allPages = [...(initialPages?.items ?? []), ...(extraPages?.items ?? [])];
  const nextPage = (extraPages ?? initialPages)?.next_after;
  const pageNumber = selection ?? allPages[0]?.file_page;
  const currentPage = allPages.find(row => row.file_page === pageNumber) ?? (jumpedPage?.file_page === pageNumber ? jumpedPage : undefined);
  const byId = new Map(blocks.items.map(block => [block.id, block]));
  const childGroups = new Map<string, Block[]>();
  for (const block of blocks.items) {
    if (!block.parent_block_id) continue;
    const children = childGroups.get(block.parent_block_id) ?? [];
    children.push(block);
    childGroups.set(block.parent_block_id, children);
  }
  const modern = Boolean(blocks.ir_schema_version);
  const sourceVerification = blocks.source_verification;
  const issues = blocks.source_qc?.issues ?? [];
  const relations = blocks.relations ?? [];
  const requiredRelations = new Set(blocks.source_qc?.required_relation_refs);
  const missingCritical = new Set(sourceVerification?.missing_critical_refs);
  const visibleBlocks = modern
    ? blocks.items.filter(block => pageNumber === "structure" ? !blockPage(block) : blockPage(block) === pageNumber)
    : blocks.items;
  const visibleIds = new Set(visibleBlocks.map(block => block.id));
  const visibleRelations = relations.filter(relation => visibleIds.has(relation.from_block_id) || visibleIds.has(relation.to_block_id));
  const visibleIssues = issues.filter(issue => pageNumber === "structure" ? !issue.file_page : issue.file_page === pageNumber);

  useEffect(() => {
    if (focusTarget) document.getElementById(focusTarget.elementId)?.focus();
  }, [focusTarget]);

  function jumpToBlock(block: Block) {
    jumpTo(blockPage(block), `review-block-${block.id}`);
  }
  function jumpToIssue(issue: SourceIssue) {
    const block = issue.block_id ? byId.get(issue.block_id) : undefined;
    if (block) { jumpToBlock(block); return; }
    jumpTo(issue.file_page ?? undefined, issue.relation_id ? `review-relation-${issue.relation_id}` : "pdf-page-title");
  }
  function jumpTo(page: number | undefined, elementId: string) {
    function showTarget() {
      setSelection(page ?? "structure");
      setFocusTarget({ elementId });
    }
    if (!page || allPages.some(row => row.file_page === page) || jumpedPage?.file_page === page) { showTarget(); return; }
    void perform(async () => {
      const response = await readParsePages(run, page - 1, 1);
      setJumpedPage(response.items.find(row => row.file_page === page));
      showTarget();
    });
  }
  async function loadMore(after: number) {
    const more = await readParsePages(run, after);
    setExtraPages(previous => ({ ...more, items: [...(previous?.items ?? []), ...more.items] }));
    if (more.items.length) {
      setSelection(more.items[0].file_page);
      setFocusTarget({ elementId: "pdf-page-title" });
    }
  }

  return <>
    <section className="pdf-review-section" aria-labelledby="pdf-milestones-title">
      <h3 id="pdf-milestones-title">技术处理与人工核验</h3>
      <dl className="pdf-milestones">
        <div><dt>原始解析</dt><dd>{blocks.milestones?.parsed || current.artifact_manifest_ref ? "已归档" : modern || current.processing_stage ? "尚未归档" : "无历史记录"}</dd></div>
        <div><dt>归一化</dt><dd>{blocks.milestones?.normalized ? "已对齐原页" : modern ? "未完成" : "尚无原页投影"}</dd></div>
        <div><dt>结构恢复</dt><dd>{blocks.milestones?.structured ? "已恢复" : modern ? "未完成" : "尚无结构投影"}</dd></div>
        <div><dt>源质量检查</dt><dd>{blocks.source_qc ? processingLabel(blocks.source_qc.status, stateLabel(blocks.source_qc.status)) : "尚无检查报告"}</dd></div>
        <div><dt>源人工核验</dt><dd>{sourceVerification?.state === "verified" ? "已核验" :
          sourceVerification?.state === "rejected" ? "已退回" : modern ? "待核验" : "按历史内容核验"}</dd></div>
      </dl>
      {!!sourceVerification?.reasons.length && <ul className="pdf-review-reasons">
        {sourceVerification.reasons.map(reason => <li key={reason}>{processingLabel(reason, "源核验仍有未解决事项。")}</li>)}
      </ul>}
      {sourceVerification && <button className="ghost" onClick={openConfirm}>{sourceVerification.state === "verified" ? "查看业务确认依据" : "转到业务依据"}</button>}
      {issues.length > 0 && <details className="pdf-issues">
        <summary>查看 {issues.length} 项源质量问题</summary>
        <IssueList issues={issues} onJump={jumpToIssue} />
      </details>}
    </section>
    {modern ? <section className="pdf-review-section" aria-labelledby="pdf-pages-title">
      <div className="pdf-page-heading"><h3 id="pdf-pages-title">逐页对照原文</h3>
        {typeof initialPages?.total_page_count === "number" && <span className="locator">共 {initialPages.total_page_count} 页</span>}</div>
      {!allPages.length && <p className="muted">原页清单尚未就绪，请刷新批次查看处理进度。</p>}
      {allPages.length > 0 && <div className="pdf-page-workbench">
        <nav className="pdf-page-nav" aria-label="原页清单">
          <ul className="pdf-page-list">
            {allPages.map(page => <li key={page.file_page}><button type="button" className="ghost" aria-pressed={pageNumber === page.file_page}
              onClick={() => setSelection(page.file_page)}><strong>第 {page.file_page} 页</strong><span>{processingLabel(page.state, stateLabel(page.state))}</span></button></li>)}
          </ul>
          {typeof nextPage === "number" && <button className="ghost" onClick={() => void perform(() => loadMore(nextPage))}>加载更多原页</button>}
          {blocks.items.some(block => block.structure_role === "container") &&
            <button className="ghost" aria-pressed={pageNumber === "structure"} onClick={() => setSelection("structure")}>查看文档结构</button>}
        </nav>
        <div className="pdf-page-content">
          <h4 id="pdf-page-title" tabIndex={-1}>{pageNumber === "structure" ? "文档结构" : `PDF 第 ${pageNumber} 页`}</h4>
          {typeof pageNumber === "number" && <OriginalLink base={base} page={pageNumber} />}
          {currentPage && <p className="locator">
            {processingLabel(currentPage.state, stateLabel(currentPage.state))} · {processingLabel(currentPage.page_class)} · {currentPage.printed_page ?
              `印刷页码 ${currentPage.printed_page}${currentPage.printed_page_basis ? "（已有定位依据）" : "（依据待核对）"}` : "印刷页码待核对"}
            {currentPage.coverage.blank_basis === "no_text_images_or_drawings" && " · 未发现原生文字、图片或图形"}
          </p>}
          {currentPage && Boolean(currentPage.coverage.image_candidate_count || currentPage.coverage.vector_candidate_count) &&
            <p className="locator">原页清点 {currentPage.coverage.image_candidate_count ?? 0} 项图片、{currentPage.coverage.vector_candidate_count ?? 0} 项图形</p>}
          {currentPage?.state === "blank" && <p className="muted">此页有空白判定依据，可打开 PDF 复核。</p>}
          {typeof pageNumber === "number" && !currentPage && <p className="muted">通过内容定位到此页。完整页处置可在原页清单中加载。</p>}
          {visibleIssues.length > 0 && <IssueList issues={visibleIssues} onJump={jumpToIssue} />}
          {visibleRelations.length > 0 && <section aria-label="本页适用关系" className="pdf-relations">
            <h4>适用关系</h4>
            <ul>{visibleRelations.map(relation => <RelationRow key={relation.id} relation={relation}
              byId={byId} required={requiredRelations.has(relation.id)} onJump={jumpToBlock} />)}</ul>
          </section>}
          {visibleBlocks.length === 0 && currentPage?.state !== "blank" && <p className="muted">此页尚无可核验内容，请对照原页处置和问题。</p>}
          <div className="blocks">
            {visibleBlocks.map(block => <BlockReview key={`${block.id}-${block.revision}`} block={block} run={run} base={base} immutable={immutable}
              critical={blocks.source_qc?.critical_refs?.[block.id] ?? []} confirmationMissing={missingCritical.has(block.id)}
              parent={block.parent_block_id ? byId.get(block.parent_block_id) : undefined} children={childGroups.get(block.id) ?? []}
              onJump={jumpToBlock} onSave={(choice, reason) => saveBlock(block, choice, reason)} />)}
          </div>
        </div>
      </div>}
    </section> : <div className="blocks">
      {visibleBlocks.map(block => <BlockReview key={`${block.id}-${block.revision}`} block={block} run={run} base={base}
        immutable={immutable} critical={[]} confirmationMissing={false} onJump={jumpToBlock} children={[]}
        onSave={(choice, reason) => saveBlock(block, choice, reason)} />)}
      {!visibleBlocks.length && <p className="muted">尚无可核验内容块。请刷新查看后台处理进度。</p>}
    </div>}
  </>;

  function saveBlock(block: Block, choice: string, reason: string) {
    return perform(async () => {
      await api.mutate(`/parse-runs/${id(run)}/blocks/${id(block.id)}/review`, { review_status: choice, reason }, etag(block));
      await onChange();
      setFocusTarget({ elementId: `review-block-${block.id}` });
    }, "逐块核验结果已保存。");
  }
}

function IssueList({ issues, onJump }: { issues: SourceIssue[]; onJump: (issue: SourceIssue) => void }) {
  return <ul className="pdf-issue-list">
    {issues.map(issue => <li key={issue.id}>
      <span>{issue.severity === "blocking" ? "阻断" : "待核对"}{issue.file_page && ` · 第 ${issue.file_page} 页`} · {processingLabel(
        issue.code === "QC_UPSTREAM_ISSUE_UNRESOLVED" ? issue.basis?.code : issue.code, "来源或结构有待核对。"
      )}</span>
      {(issue.file_page || issue.block_id || issue.relation_id) && <button className="ghost" onClick={() => onJump(issue)}>定位问题</button>}
    </li>)}
  </ul>;
}

function partName(block: Block, part: string) {
  if (!part) return "整块内容";
  const keys = part.split("/").slice(1);
  if (!sliceKeys.includes(keys[0])) return "块内片段";
  let value: unknown = block.structured_content;
  for (const key of keys) {
    value = value && typeof value === "object" && Object.hasOwn(value, key) ? (value as Record<string, unknown>)[key] : undefined;
  }
  if (keys[0] === "mathml_raw") return "原公式表达式";
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const item = value as Record<string, unknown>;
    if (typeof item.row === "number" && typeof item.column === "number") return `第 ${item.row + 1} 行第 ${item.column + 1} 列${typeof item.text === "string" && item.text ? ` · ${item.text}` : ""}`;
    for (const field of ["text", "value", "symbol"]) if (typeof item[field] === "string") return item[field];
  }
  return keys[0] === "asset_parts" ? "块内图像" : "块内片段（请对照原文）";
}

function RelationRow({ relation, byId, required, onJump }: { relation: BlockRelation; byId: Map<string, Block>; required: boolean; onJump: (block: Block) => void }) {
  const from = byId.get(relation.from_block_id), to = byId.get(relation.to_block_id);
  const scope = relation.basis.scope;
  const scopeRefs = scope ? ["section_id", "procedure_id", "group_id", "table_id", "visual_group_id"]
    .map(key => typeof scope[key] === "string" ? byId.get(scope[key]) : undefined).filter((block): block is Block => Boolean(block)) : [];
  const scopeText = [scope?.document_root_id ? "全文" : null, ...scopeRefs.map(blockName),
    typeof scope?.step_number === "number" ? `步骤 ${scope.step_number}` : null,
    typeof scope?.symbol === "string" ? `变量 ${scope.symbol}` : null,
    scope?.axis === "row" ? "按行适用" : scope?.axis === "column" ? "按列适用" : null,
    typeof scope?.parameter_part === "string" && from ? `参数 ${partName(from, scope.parameter_part)}` : null].filter(Boolean).join(" · ");
  return <li id={`review-relation-${relation.id}`} tabIndex={-1} className="pdf-relation">
    <p><strong>{processingLabel(relation.relation_type, "原文关系")}</strong> · {processingLabel(relation.state)}{required && " · 必检"}</p>
    <dl>
      {[{ label: "起点", block: from, part: relation.from_part }, { label: "终点", block: to, part: relation.to_part }].map(({ label, block, part }) =>
        <div key={label}><dt>{label}</dt><dd>{block ? <>
          <button type="button" className="ghost" onClick={() => onJump(block)}>{blockName(block)}</button> <span>{partName(block, part)}</span>
        </> : "来源当前不可定位"}</dd></div>)}
      <div><dt>范围</dt><dd>{scopeText || "适用范围待核对"}</dd></div>
    </dl>
  </li>;
}
