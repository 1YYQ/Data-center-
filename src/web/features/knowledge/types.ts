import { stateLabel } from "../../labels";
export type Version = { id: string; revision: number; revision_label: string | null; version_no: number; publish_status: string; reference_access_state: string; selected_chunk_set_id?: string | null; selected_review_set_id?: string | null };
type TableCell = { text: string; row?: number; column?: number; header?: boolean; header_scope?: "row" | "col" | "rowgroup" | "colgroup"; rowspan?: number; colspan?: number; graphic_keys?: { label: string; operation: string }[] };
type CellPosition = { row: number; column: number };
export type SourceRegion = { file_page: number; printed_page?: string | null; geometry_status: string; bbox_normalized?: number[] };
export type Block = {
  id: string;
  revision: number;
  ordinal: number;
  kind: string;
  review_status: string;
  text: string;
  locator: { file_page?: number; printed_page?: string | null; extraction?: string; bbox_normalized?: number[]; regions?: SourceRegion[]; section?: string };
  review_reason: string | null;
  content_hash: string;
  file_page?: number | null;
  source_kind?: string | null;
  structure_role?: "leaf" | "container" | "observation" | null;
  parent_block_id?: string | null;
  structured_content?: {
    rows?: TableCell[][];
    caption?: { text: string };
    captions?: string[];
    footnotes?: string[];
    recovered_warnings?: CellPosition[];
    recovered_icons?: CellPosition[];
    pending_graphic_keys?: CellPosition[];
    asset_parts?: { artifact_id: string; cell_ref?: CellPosition | null }[];
    source_parts?: { text?: string; role?: string }[];
    [part: string]: unknown;
  };
};
export type ParseRun = {
  id: string;
  status: string;
  was_published?: boolean;
  origin?: string;
  error_code?: string | null;
  processing_stage?: string | null;
  stage_state?: string;
  processed_page_count?: number;
  total_page_count?: number | null;
  artifact_manifest_ref?: string | null;
  created_at?: string | null;
};
export type ParsePage = {
  file_page: number;
  printed_page: string | null;
  printed_page_basis: unknown;
  page_class: string;
  state: string;
  coverage: { native_text_present?: boolean; image_candidate_count?: number; vector_candidate_count?: number; blank_basis?: string | null };
  issue_refs: string[];
};
export type ParsePages = { items: ParsePage[]; next_after: number | null; total_page_count: number | null; ir_schema_version?: string | null };
export type BlockRelation = {
  id: string;
  relation_type: string;
  from_block_id: string;
  to_block_id: string;
  from_part: string;
  to_part: string;
  basis: { rule?: string; scope?: Record<string, unknown> };
  state: string;
};
export type SourceIssue = { id: string; severity: string; code: string; file_page?: number | null; block_id?: string; relation_id?: string; basis?: { code?: string } };
type SourceVerification = {
  state: string;
  reasons: string[];
  missing_leaf_refs?: string[];
  missing_critical_refs?: string[];
  missing_relation_refs?: string[];
  missing_observation_refs?: string[];
  confirmation_refs?: string[];
};
export type ParseBlocks = {
  items: Block[];
  was_published?: boolean;
  ir_schema_version?: string | null;
  relations?: BlockRelation[];
  milestones?: { parsed: boolean; normalized: boolean; structured: boolean; source_qc_passed: boolean; source_verified: boolean };
  source_qc?: { status: string; current: boolean; issues: SourceIssue[]; critical_refs: Record<string, string[]>; required_relation_refs: string[] };
  source_verification?: SourceVerification;
};
export type ChunkSet = {
  id: string;
  status: string;
  splitter_version: string;
  quality_status?: string;
  quality_current?: boolean | null;
  quality_issues?: { code: string }[];
  error_code?: string | null;
  selected?: boolean;
  review_set_ids?: string[];
  child_count?: number;
  parent_count?: number;
};
export type ReviewSet = {
  id: string;
  parse_run_id: string;
  chunk_set_id: string;
  coverage_state: string;
  is_ir?: boolean;
  selected?: boolean;
  reviewed_at?: string | null;
};
export type ReviewSets = { items: ReviewSet[]; next_cursor?: string | null };
export const blockPage = (block: Block) => block.file_page ?? block.locator.file_page ?? block.locator.regions?.[0]?.file_page;
export type SourceBinding = {
  parse_run_id: string;
  ir_artifact_id: string;
  source_qc_ref: string | null;
  source_projection_hash: string;
  block_bindings: Record<string, string>;
  container_bindings: Record<string, string>;
  observation_bindings: Record<string, string>;
  page_bindings: Record<string, string>;
  relation_bindings: Record<string, string>;
};
export type ObservationDisposition =
  | { decision: "covered_by"; reason: string; block_refs: string[] }
  | { decision: "decorative" | "out_of_scope"; reason: string }
  | { decision: "image_only_accepted"; reason: string; policy_ref: string };
export type Authority = { id: string; identity_label: string; responsibility: string; purposes: string[]; identity: Record<string, unknown> };
export type Confirmation = {
  id: string; etag: string; kind: string; conclusion: string; current: boolean; verification_state: string;
  actual_confirmer: { identity_label: string; authority_basis_ref: string }; confirmed_at: string; proof_refs: string[];
  scope: { parse_run_id?: string; [key: string]: unknown };
  supersedes_confirmation_id?: string | null;
};
export type ConfirmationOptions = {
  classification: { access_level: string | null; category: string | null; need_class: string | null };
  source_binding: Record<string, unknown>;
  authority_bases: Authority[];
  run_source_bindings?: Record<string, SourceBinding>;
  run_block_bindings?: Record<string, Record<string, string>>;
  quality_holds?: { id: string; reason: string; affected_refs: string[]; revision: number }[];
};
export type Evidence = { id: string; source_description: string; attested: boolean; has_file: boolean; linked?: boolean };
export type VerificationOptions = { evidence: Evidence[]; proof_refs: string[]; revision: number };
export type Preview = { action: string; allowed: boolean; preview_id: string; blocked_reasons: string[]; expires_at: string; document_etag: string; impact: Record<string, unknown> };
export const versionName = (version: Version) => `${version.revision_label || "版本 " + version.version_no} · ${stateLabel(version.publish_status)}`;
export const kindName: Record<string, string> = { applicability: "业务适用性", key_content: "关键内容专业确认", classification: "资料分类" };
