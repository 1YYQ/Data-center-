import { useState } from "react";
import { api, id } from "../../api";
import { formValues, text } from "../../components";
import { processingLabel } from "./processingLabels";
import { blockPage } from "./types";
import type { Block } from "./types";

const correctableKinds = new Set(["paragraph", "title", "step", "warning", "header", "footer", "footnote", "caption", "ref_text", "aside_text", "page_number"]);
type TextCorrection = { block_id: string; text?: string; kind?: string; reason: string; basis: { file_page: number; description: string } };

export function OriginalLink({ base, page, label }: { base: string; page?: number; label?: string }) {
  return <a href={base + "/file" + (page ? "#page=" + page : "")} target="_blank" rel="noopener noreferrer">
    {label ?? (page ? `打开原文第 ${page} 页` : "打开此版本原文 PDF")}
  </a>;
}

export function blockName(block: Block) {
  const page = blockPage(block);
  return `${page ? `第 ${page} 页 · ` : ""}${processingLabel(block.kind, "内容块")} ${block.ordinal}`;
}

function SourceImage({ src, run, alt }: { src: string; run: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  const [loaded, setLoaded] = useState(false);
  if (failed) return <span role="status" className="muted">
    原图暂不可读取，请核对原页。 <button type="button" className="ghost" onClick={() => setFailed(false)}>重试读取原图</button>
  </span>;
  return <>
    {!loaded && <span role="status" className="locator">正在读取原图…</span>}
    <img src={src} alt={alt} loading="lazy" onLoad={() => setLoaded(true)} onError={() => {
      setFailed(true); setLoaded(false);
      // A missing crop and revoked access both intentionally return 404.
      void api.get(`/parse-runs/${id(run)}`).catch(() => {});
    }} />
  </>;
}

function SourcePreview({ run, src, label, alt }: { run: string; src: string; label: string; alt: string }) {
  const [open, setOpen] = useState(false);
  return <details className="table-source" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    {open && <SourceImage key={src} run={run} src={src} alt={alt} />}
  </details>;
}

function RecognizedTable({ block, run }: { block: Block; run: string }) {
  const table = block.structured_content;
  if (!table?.rows?.length) return null;
  const captions = table.caption?.text || table.captions?.join(" ");
  return <div className="block-table">
    <table aria-label={`内容块 ${block.ordinal} 识别表格`}>
      {captions && <caption>{captions}</caption>}
      <tbody>{table.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => {
        if (cell.header) return <th key={cellIndex} scope={cell.header_scope ?? (rowIndex === 0 ? "col" : "row")}
          rowSpan={cell.rowspan} colSpan={cell.colspan}>{cell.text}</th>;
        const matches = (entry: { row: number; column: number }) => entry.row === rowIndex && entry.column === cellIndex;
        const warning = table.recovered_warnings?.some(matches);
        const icon = table.recovered_icons?.some(matches);
        const needsKeyReview = table.pending_graphic_keys?.some(matches);
        const reviewLabel = block.review_status === "approved" ? "" : " · 待对照";
        return <td key={cellIndex} rowSpan={cell.rowspan} colSpan={cell.colspan}>
          {warning && <strong className="table-recovered">补识警示{reviewLabel}</strong>}
          {needsKeyReview && <strong className="table-recovered">图形键待核验</strong>}
          {icon && <strong className="table-recovered">补识按键符号{reviewLabel}</strong>}
          {cell.text}
          {!!cell.graphic_keys?.length && <ul className="cell-graphic-keys">
            {cell.graphic_keys.map((graphic, keyIndex) => <li key={keyIndex}>
              <SourceImage src={`/parse-runs/${id(run)}/blocks/${id(block.id)}/source-image?row=${rowIndex}&column=${cellIndex}&key=${keyIndex}`}
                alt={`${graphic.label}原图`} run={run} />
              <span>{graphic.label} · {graphic.operation}</span>
            </li>)}
          </ul>}
        </td>;
      })}</tr>)}</tbody>
    </table>
    {table.footnotes?.map((note, index) => <p key={index}>{note}</p>)}
  </div>;
}

export function BlockReview({ block, run, base, immutable, critical, confirmationMissing, onSave, onCorrect, onJump, parent, children }: {
  block: Block;
  run: string;
  base: string;
  immutable: boolean;
  critical: string[];
  confirmationMissing: boolean;
  onSave: (choice: string, reason: string) => Promise<void>;
  onCorrect?: (edit: TextCorrection) => Promise<void>;
  onJump: (block: Block) => void;
  parent?: Block;
  children: Block[];
}) {
  const [choice, setChoice] = useState(block.review_status), [reason, setReason] = useState(block.review_reason || "");
  const { extraction, regions, printed_page } = block.locator;
  const page = blockPage(block);
  const pendingKeys = block.kind === "table" ? block.structured_content?.pending_graphic_keys ?? [] : [];
  const visualSource = block.kind === "image" && extraction === "pdf_visual_inventory";
  const legacyCrop = ((block.kind === "table" && extraction === "mineru_basic_ocr") || visualSource) && Array.isArray(block.locator.bbox_normalized);
  const sourceImage = `/parse-runs/${id(run)}/blocks/${id(block.id)}/source-image`;
  const printing = printed_page ?? regions?.[0]?.printed_page;
  const observation = block.structure_role === "observation";
  const container = block.structure_role === "container";
  const hasReview = !observation && !container;
  const assets = block.structured_content?.asset_parts ?? [];
  return <article className="block pdf-review-block" id={`review-block-${block.id}`} data-block-id={block.id} tabIndex={-1}>
    <div className="block-head">
      <strong>{blockName(block)} · {container ? "结构容器" : observation ? "须据实处置" : processingLabel(block.review_status)}</strong>
      {hasReview && !immutable && <select aria-label={`内容块 ${block.ordinal} 核验结论`} value={choice} onChange={event => setChoice(event.target.value)}>
        <option value="unreviewed">待核验</option><option value="approved" disabled={pendingKeys.length > 0}>核验通过</option><option value="rejected">退回</option>
      </select>}
    </div>
    {block.kind === "table" && block.structured_content?.rows?.length ? <RecognizedTable block={block} run={run} /> :
      block.text && <blockquote>{block.text}</blockquote>}
    {critical.length > 0 && <p className="locator">关键内容 {critical.map(tag => processingLabel(tag, "其他")).join("、")}{confirmationMissing && " · 缺少有效专业确认"}</p>}
    {page && <p className="locator">PDF 第 {page} 页 · {printing ? `印刷页码 ${printing}` : "印刷页码待核对"}{extraction === "mineru_basic_ocr" && " · OCR"}
      {block.source_kind === "manual" && " · 人工修正内容"}</p>}
    {page ? <OriginalLink base={base} page={page} /> : !container && <p className="locator">{block.locator.section || "原文页序未知"}</p>}
    {parent && <p className="locator">所属结构 <button type="button" className="ghost" onClick={() => onJump(parent)}>{blockName(parent)}</button></p>}
    {container && <p className="muted">按子项覆盖、阅读顺序和适用关系核验此结构。</p>}
    {children.length > 0 && <details><summary>查看 {children.length} 个直接子项</summary>
      <ul className="pdf-link-list">{children.map(child => <li key={child.id}><button className="ghost" onClick={() => onJump(child)}>{blockName(child)}</button></li>)}</ul>
    </details>}
    {observation && <p className="muted">对照原页后，在「业务依据」记录实际确认人提供的处置依据。</p>}
    {(regions || []).map((region, index) => ["exact", "table_region_only"].includes(region.geometry_status) && Array.isArray(region.bbox_normalized)
      ? <SourcePreview key={index} run={run} src={sourceImage + `?region=${index}`}
        label={`核对原页区域 · 第 ${region.file_page} 页${regions!.length > 1 ? `，区域 ${index + 1}` : ""} · ${processingLabel(region.geometry_status)}`}
        alt={`PDF 第 ${region.file_page} 页原文裁图`} />
      : <p className="locator" key={index}>第 {region.file_page} 页 · {processingLabel(region.geometry_status)}，请核对整页。</p>)}
    {legacyCrop && <SourcePreview run={run} src={sourceImage} label={visualSource ? "核对原页图形" : "核对原表与按键图标"}
      alt={`PDF 第 ${page} 页原文裁图`} />}
    {assets.map((asset, index) => <SourcePreview key={index} run={run} src={`/parse-runs/${id(run)}/assets/${id(asset.artifact_id)}`}
      label={asset.cell_ref ? `核对表内图像 · 第 ${asset.cell_ref.row + 1} 行第 ${asset.cell_ref.column + 1} 列` : `核对归档图像 ${index + 1}`}
      alt={`${blockName(block)}的归档图像`} />)}
    {pendingKeys.length > 0 && <p className="muted">请对照原表校正图形键后再提交核验。</p>}
    {hasReview && (immutable ? block.review_reason && <p className="muted">核验说明 {block.review_reason}</p> : <>
      <input aria-label={`内容块 ${block.ordinal} 核验说明`} value={reason} onChange={event => setReason(event.target.value)} placeholder="核验说明" />
      <button className="ghost" onClick={() => void onSave(choice, reason)}>保存核验</button>
    </>)}
    {onCorrect && !immutable && page && block.structure_role === "leaf" && correctableKinds.has(block.kind) &&
      (block.source_kind === "parser" || block.source_kind === "manual") && <details>
        <summary>按原文修正此块</summary>
        <form className="stack" onSubmit={event => {
          event.preventDefault();
          const values = formValues(event);
          const edited = String(values.get("corrected_text") ?? "");
          const kind = text(values, "corrected_kind");
          void onCorrect({ block_id: block.id,
            ...(edited !== block.text ? { text: edited } : {}), ...(kind ? { kind } : {}),
            reason: text(values, "reason"), basis: { file_page: page, description: text(values, "source_basis") } });
        }}>
          <label>修正后文字<textarea name="corrected_text" rows={4} defaultValue={block.text} required maxLength={12000} /></label>
          {["paragraph", "ref_text", "aside_text"].includes(block.kind) && <label>修正内容类型<select name="corrected_kind">
            <option value="">沿用当前类型</option><option value="warning">警告</option>
            {block.kind !== "aside_text" && <option value="step">操作步骤</option>}
          </select></label>}
          <label>修正原因<input name="reason" required maxLength={500} /></label>
          <label>原页对照依据<input name="source_basis" required maxLength={1000} /></label>
          <div><button type="submit" className="ghost">提交修正批次</button></div>
        </form>
      </details>}
  </article>;
}
