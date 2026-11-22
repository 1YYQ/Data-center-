import { useState } from "react";
import { api, id } from "../../api";
import { ProtectedImage } from "../../ProtectedImage";
import { SourceLink } from "../../SourceReader";
import "./evidence.css";

type SourcePosition = {
  file_page?: number | null;
  printed_page?: string | null;
  section?: string | null;
  geometry_status?: string | null;
  bbox_normalized?: number[] | null;
};
type CellRange = { row_start: number; row_end: number; column_start: number; column_end: number };
type SourceSpan = SourcePosition & { role?: string; step_number?: number | null; cell_range?: CellRange | null };
export type Evidence = SourcePosition & {
  id: string;
  document_title: string;
  quote: string | null;
  version: { id: string; document_id: string; revision_label?: string | null; version_no: number; publish_status: string };
  locator?: (SourcePosition & { region?: SourcePosition | null; regions?: SourcePosition[]; cell_range?: CellRange | null }) | null;
  evidence_kind?: "text" | "region" | null;
  role?: string | null;
  step_number?: number | null;
  source_spans?: SourceSpan[];
  asset_refs?: string[];
};

const roleLabels: Record<string, string> = {
  warning: "警告", condition: "适用条件", definition: "定义", header: "表头", context: "相关说明",
};

function isPositiveInteger(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function sourcePages(evidence: Evidence) {
  const locator = evidence.locator;
  const candidates = [locator?.region, ...(evidence.source_spans ?? []), ...(locator?.regions ?? []), locator, evidence];
  const places = new Map<number, SourcePosition>();
  for (const entry of candidates) {
    if (!entry || !isPositiveInteger(entry.file_page)) continue;
    const known = places.get(entry.file_page);
    places.set(entry.file_page, {
      file_page: entry.file_page, printed_page: known?.printed_page || entry.printed_page,
      section: known?.section || entry.section,
    });
  }
  return [...places.entries()];
}

function cropAvailable(region: SourcePosition | null | undefined) {
  if (!region) return false;
  const rect = region.bbox_normalized;
  return isPositiveInteger(region.file_page) && (region.geometry_status === "exact" || region.geometry_status === "table_region_only")
    && rect?.length === 4 && rect.every(value => Number.isFinite(value) && value >= 0 && value <= 1)
    && rect[0] < rect[2] && rect[1] < rect[3];
}

function cellText(range: CellRange | null | undefined) {
  if (!range) return "";
  const { row_start: top, row_end: bottom, column_start: left, column_end: right } = range;
  if (![top, bottom, left, right].every(value => Number.isSafeInteger(value) && value >= 0) || top >= bottom || left >= right) return "";
  const rows = top + 1 === bottom ? String(bottom) : `${top + 1}–${bottom}`;
  const columns = left + 1 === right ? String(right) : `${left + 1}–${right}`;
  return `表格第 ${rows} 行 · 第 ${columns} 列`;
}

function EvidenceImage({ path, src, label, alt }: { path: string; src: string; label: string; alt: string }) {
  const [open, setOpen] = useState(false);
  return <details className="query-evidence-preview" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    {open && <EvidenceImageContent key={src} path={path} src={src} alt={alt} />}
  </details>;
}

function EvidenceImageContent({ path, src, alt }: { path: string; src: string; alt: string }) {
  return <ProtectedImage src={src} accessPath={path} alt={alt}/>;
}

export function EvidenceCard({ evidence, taskId }: { evidence: Evidence; taskId: string }) {
  const { version, locator } = evidence;
  const pdf = `/workspace/manuals?document=${id(version.document_id)}&version=${id(version.id)}&task=${id(taskId)}&evidence=${id(evidence.id)}`;
  const path = `/tasks/${id(taskId)}/evidence/${id(evidence.id)}`;
  const pages = sourcePages(evidence);
  const regions = locator?.region ? [locator.region] : locator?.regions ?? [];
  const canPreview = Boolean(taskId && evidence.id);
  const hasCrop = canPreview && regions.some(cropAvailable);
  const isRegion = evidence.evidence_kind === "region";
  const assets = isRegion ? [...new Set(evidence.asset_refs ?? [])] : [];
  const commonSection = locator?.section || evidence.section;
  const printing = locator?.printed_page || evidence.printed_page;
  const cells = cellText(locator?.cell_range) || cellText(evidence.source_spans?.[0]?.cell_range);
  const step = evidence.step_number ?? evidence.source_spans?.find(span => isPositiveInteger(span.step_number))?.step_number;
  const sourceRole = evidence.role ?? evidence.source_spans?.[0]?.role ?? "";
  const role = isPositiveInteger(step)
    ? [`步骤 ${step}`, roleLabels[sourceRole]].filter(Boolean).join(" · ")
    : roleLabels[sourceRole];
  const historical = version.publish_status === "superseded" || version.publish_status === "withdrawn";
  return <article className="evidence query-evidence" data-evidence-kind={isRegion ? "region" : "text"}>
    <h3>{evidence.document_title} · {version.revision_label || "版本标识待确认"}{historical && "（历史版本）"}</h3>
    {(role || cells || isRegion) && <p className="locator">{[role, isRegion ? "图像证据" : null, cells].filter(Boolean).join(" · ")}</p>}
    {!isRegion && evidence.quote && <blockquote>{evidence.quote}</blockquote>}
    {isRegion && !pages.length && <p className="muted">此图像的原页位置待核对。</p>}
    {pages.length ? <ul className="query-evidence-pages">
      {pages.map(([page, place]) => <li key={page}>
        <SourceLink documentId={version.document_id} versionId={version.id} page={page} evidencePath={path}>核对原文 · PDF 第 {page} 页</SourceLink>
        {place.printed_page && <span>印刷页码 {place.printed_page}</span>}
        {(place.section || (pages.length === 1 && commonSection)) && <span>{place.section || commonSection}</span>}
        {regions.some(region => region?.file_page === page && region.geometry_status === "page_only")
          && !regions.some(region => region?.file_page === page && cropAvailable(region)) && <span>仅定位至原页</span>}
      </li>)}
    </ul> : <p className="query-evidence-pages">
      <SourceLink documentId={version.document_id} versionId={version.id} evidencePath={path}>打开同版原文 PDF</SourceLink>
      <span>{printing ? `印刷页码 ${printing} · PDF 页序待核对` : "原文页序待核对"}</span>
      {commonSection && <span>{commonSection}</span>}
    </p>}
    {hasCrop && regions.map((region, regionIndex) => cropAvailable(region) && <EvidenceImage key={regionIndex} path={path}
      src={path + `/source-image?region=${regionIndex}`}
      label={`查看原页裁图 · 第 ${region.file_page} 页${region.geometry_status === "table_region_only" ? " · 原表区域" : ""}`}
      alt={`PDF 第 ${region.file_page} 页的${region.geometry_status === "table_region_only" ? "原表" : "引用区域"}裁图`} />)}
    {canPreview && assets.map((asset, index) => <EvidenceImage key={asset} path={path} src={path + "/assets/" + id(asset)}
      label={assets.length === 1 ? "查看图像证据" : `查看图像证据 ${index + 1}`} alt={`${evidence.document_title}的原文图像`} />)}
    <a href={pdf}>查阅手册与反馈</a>
  </article>;
}
