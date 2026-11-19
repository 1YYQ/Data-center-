import { useEffect, useState } from "react";
import { api, id } from "../../api";
import { switchIndexGeneration } from "./indexApi";
import type { IndexStatus } from "./indexApi";
import "./indexManagement.css";

type Perform = (action: () => Promise<void>, success?: string) => Promise<void>;
type Profile = { id: string; model_name: string; model_revision?: string | null; dimension?: number };
type Profiles = { items: Profile[]; legacy_mode_available: boolean };
type IndexIssue = { version_id?: string; document_version_id?: string; code?: string; reason?: string };
type Batch = { id: string | null; batch_no: number; state: string; error_code?: string | null; can_accept: boolean };
type Generation = {
  id: string; kind: string; embedding_profile_id: string; status: string; stale: boolean; active: boolean;
  source_revision: number; expected_chunk_count: number; actual_chunk_count?: number | null;
  created_at?: string | null; manifest_hash?: string | null; activation_count?: number;
  integrity?: {
    status?: string; error_code?: string; blockers?: IndexIssue[];
    missing_child_refs?: string[]; unexpected_child_refs?: string[]; invalid_child_refs?: string[];
  } | null;
  exclusions?: IndexIssue[];
  embedding_batches?: Batch[];
  batch_count?: number;
};
type GenerationPage = { items: Generation[]; next_cursor?: string | null };
type Snapshot = { status: IndexStatus; profiles: Profiles | null; generations: GenerationPage | null };
type BuildReceipt = { index_generation_id: string; status: string };

const indexStates: Record<string, string> = {
  ready: "可用", stale: "来源已变化", not_ready: "尚未就绪", missing: "尚未就绪",
  building: "构建中", failed: "构建失败", retired: "已退役", ready_partial: "历史部分索引",
};
const batchStates: Record<string, string> = {
  not_accepted: "未受理", pending: "等待后台处理", running: "处理中", succeeded: "已完成",
  failed: "处理失败", reconciliation_required: "结果待对账",
};
const indexIssues: Record<string, string> = {
  no_current_published_sources: "知识库尚无现行发布的资料。",
  no_verified_vector_child: "缺少通过核验的可检索片段。",
  publication_not_current: "资料发布资格已失效。",
  publication_not_qualified: "资料尚未满足当前发布资格。",
  legacy_source_not_current: "旧式资料的来源或核验已失效。",
  child_source_required: "这份资料须使用通过核验的切分集合。",
  selected_source_not_ready: "资料当前选用的来源尚未就绪。",
  legacy_source_requires_child_reprocessing: "旧版本资料须完成切分及核验后才能纳入向量索引。",
  source_verification_not_current: "资料缺少当前有效的源人工核验。",
  collection_qc_or_tokenizer_not_current: "切分集合检查或分词配置已失效。",
  publication_event_missing: "资料缺少可核对的正式发布记录。",
  multiple_effective_versions: "同一文档有多个现行版本，需先核对适用范围。",
  required_source_coverage_incomplete: "缺少正式查询必须保留的原文内容。",
  support_group_membership_incomplete: "部分检索证据缺少所依赖的原文内容。",
  asset_only_group_has_no_verified_region: "图像依据缺少已核验的定位。",
  INDEX_VECTOR_STORE_REQUIRED: "当前环境的向量存储尚未就绪。",
  INDEX_SOURCE_INCOMPLETE: "仍有必须纳入索引的资料缺少有效来源。",
  INDEX_GENERATION_STALE: "知识库来源已变化，须按当前来源构建新代际。",
  INDEX_EMBEDDING_PROFILE_CHANGED: "此代际的嵌入配置资格已变化。",
  INDEX_SOURCE_MEMBERSHIP_CHANGED: "资料成员与候选清单不再一致。",
  INDEX_VECTOR_MEMBERSHIP_CHANGED: "向量成员与完整性报告不一致。",
  SERVICE_SYNTHETIC_INPUT_REQUIRED: "当前配置仍缺少此批次所需的受控输入依据。",
  SERVICE_RECONCILIATION_REQUIRED: "原批次结果待对账。",
  SERVICE_OUTCOME_UNKNOWN: "原批次结果待对账。",
};

function generationStatus(row: Generation) {
  if (row.status === "ready") return row.stale ? "已过期，须重建" : "构建已就绪";
  return indexStates[row.status] || "状态待核对";
}

function timeLabel(value?: string | null) {
  const date = value ? new Date(value) : null;
  return date && Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "时间未知";
}

async function getSnapshot(base: string): Promise<Snapshot> {
  const status = await api.get<IndexStatus>(base + "/index-status");
  if (!status.can_manage_index) return { status, profiles: null, generations: null };
  const [profiles, generations] = await Promise.allSettled([
    api.get<Profiles>(base + "/embedding-profiles"),
    api.get<GenerationPage>(base + "/index-generations"),
  ]);
  return { status, profiles: profiles.status === "fulfilled" ? profiles.value : null,
    generations: generations.status === "fulfilled" ? generations.value : null };
}

export function IndexManagement({ kb, versionId, invalidation, visible, perform }: {
  kb: string; versionId: string; invalidation: number; visible: boolean; perform: Perform;
}) {
  const base = `/knowledge-bases/${id(kb)}`;
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [receipt, setReceipt] = useState("");
  const [profile, setProfile] = useState("");
  const [focused, setFocused] = useState<Generation | null>(null);
  const status = snapshot?.status;
  const generations = snapshot?.generations;
  const profiles = snapshot?.profiles;
  const profileOptions = profiles?.items ?? [];
  const profileName = (identifier: string) => identifier === "local-keyword" ? "本地关键词"
    : profileOptions.find(item => item.id === identifier)?.model_name || "固定嵌入配置";
  const canBuild = profileOptions.some(item => item.id === profile) || (profile === "local-keyword" && profiles?.legacy_mode_available);
  const activeId = status?.active_generation_id;
  const versionIncluded = status?.included_version_ids?.includes(versionId);

  useEffect(() => {
    if (!visible) return;
    let live = true;
    setLoading(true);
    setFocused(null);
    void getSnapshot(base)
      .then(result => { if (live) setSnapshot(result); })
      .catch(() => { if (live) setSnapshot(null); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [base, invalidation, visible]);

  function generationPath(generationId: string) { return base + "/index-generations/" + id(generationId); }
  async function showGeneration(generationId: string) {
    setFocused(await api.get<Generation>(generationPath(generationId)));
  }

  async function refresh(generationId = focused?.id) {
    setLoading(true);
    try {
      const updated = await getSnapshot(base);
      setSnapshot(updated);
      setFocused(updated.status.can_manage_index && generationId ? await api.get<Generation>(generationPath(generationId)) : null);
    } catch (error) {
      setSnapshot(null); setFocused(null);
      throw error;
    } finally {
      setLoading(false);
    }
  }

  async function switchGeneration(row: Generation, action: "activate" | "rollback") {
    if (loading || !status?.etag || !status.can_manage_index) return;
    const next = await switchIndexGeneration(base, status, row.id, action);
    setSnapshot(current => current && { ...current, status: next });
    setReceipt(`${action === "activate" ? "启用" : "回滚"}回执 · 活动代际 ${next.active_generation_id?.slice(0, 8) || "待核对"}`);
    await refresh(row.id);
  }

  return <section className="pdf-review-section index-management" aria-labelledby="index-management-title" aria-busy={loading}>
    <div className="index-heading">
      <h3 id="index-management-title">知识库索引</h3>
      <button type="button" className="ghost" disabled={loading} onClick={() => void perform(() => refresh())}>刷新索引状态</button>
    </div>
    {loading && <p role="status" className="muted">正在读取索引状态…</p>}
    {!loading && !status && <p role="status" className="warning">暂时无法读取索引状态，请刷新重试。</p>}
    {status && <div className="pdf-run-summary">
      <p><strong>索引状态 {indexStates[status.status] || "待核对"}</strong></p>
      <p className="locator">{activeId ? `活动代际 ${activeId.slice(0, 8)} · 来源修订 ${status.active_source_revision ?? "未知"}` : "尚未启用索引代际"}</p>
      {activeId && status.status === "ready" && <p className="locator">{versionIncluded ? "此版本已纳入可用索引。" : "此版本尚未纳入可用索引。"}</p>}
      {status.status === "stale" && <p className="warning">当前活动代际的资料条件已变化，须启用符合当前来源的完整代际后恢复查询。</p>}
      {status.can_manage_index && activeId && !generations?.items.some(row => row.id === activeId) &&
        <button type="button" className="ghost space" disabled={loading}
          onClick={() => void perform(() => showGeneration(activeId))}>查看活动代际</button>}
    </div>}
    {status && !status.can_manage_index && <p className="muted">构建、启用和回滚需要当前知识库的完整资料管理权限。</p>}
    {status?.can_manage_index && <>
      <div className="space">
        <h4>构建候选代际</h4>
        {!loading && !profiles && <p className="warning">暂时无法读取已准入的构建配置。</p>}
        {profiles && !profileOptions.length && <p className="muted">当前无可用的已准入嵌入配置。</p>}
        {profiles && (profileOptions.length > 0 || profiles.legacy_mode_available) && <>
          <label>索引构建配置<select value={canBuild ? profile : ""} onChange={event => setProfile(event.target.value)}>
            <option value="">请选择构建配置</option>
            {profileOptions.map(item => <option key={item.id} value={item.id}>{[item.model_name,
              `配置修订 ${item.id.slice(0, 8)}`, item.model_revision,
              item.dimension ? `${item.dimension} 维` : null].filter(Boolean).join(" · ")}</option>)}
            {profiles.legacy_mode_available && <option value="local-keyword">本地关键词（兼容旧资料）</option>}
          </select></label>
          <button type="button" className="space" disabled={loading || !canBuild} onClick={() => void perform(async () => {
            const result = await api.mutate<BuildReceipt>(base + "/index-generations", { embedding_profile_id: profile });
            const outcome = result.status === "ready" ? "构建已就绪，等待启用" : (indexStates[result.status] || "已受理");
            setReceipt(`构建回执 · 候选 ${result.index_generation_id.slice(0, 8)} · ${outcome}`);
            await refresh(result.index_generation_id);
          })}>请求构建候选代际</button>
        </>}
      </div>
      <div className="space">
        <h4>索引代际</h4>
        {!loading && !generations && <p className="warning">暂时无法读取代际列表。</p>}
        {generations && !generations.items.length && <p className="muted">此知识库暂无索引代际。</p>}
        {!!generations?.items.length && <ul className="index-generation-list">
          {generations.items.map(row => <li key={row.id}>
            <button type="button" className="ghost" aria-pressed={focused?.id === row.id} disabled={loading}
              onClick={() => void perform(() => showGeneration(row.id))}>
              <strong>{row.id.slice(0, 8)} · {profileName(row.embedding_profile_id)}</strong>
              <span>{row.id === status.active_generation_id ? "活动代际 · " : ""}{generationStatus(row)} · {timeLabel(row.created_at)}</span>
            </button>
          </li>)}
        </ul>}
        {generations?.next_cursor && <button type="button" className="ghost space" disabled={loading}
          onClick={() => void perform(async () => {
            const next = await api.get<GenerationPage>(base + "/index-generations?cursor=" + id(generations.next_cursor!));
            setSnapshot(current => current && { ...current, generations: { ...next, items: [...generations.items, ...next.items] } });
          })}>读取更多索引代际</button>}
      </div>
      {focused && <GenerationDetails row={focused} status={status} loading={loading} profileName={profileName(focused.embedding_profile_id)}
        onSwitch={action => perform(() => switchGeneration(focused, action))}
        onAcceptBatch={batch => perform(async () => {
          await api.mutate(generationPath(focused.id) + "/embedding-batches/" + batch.batch_no, {});
          setReceipt(`第 ${batch.batch_no} 批已受理，等待后台处理。`);
          await refresh(focused.id);
        })} />}
    </>}
    {receipt && <p className="status" role="status">{receipt}</p>}
  </section>;
}

function GenerationDetails({ row, status, loading, profileName, onSwitch, onAcceptBatch }: {
  row: Generation; status: IndexStatus; loading: boolean; profileName: string;
  onSwitch: (action: "activate" | "rollback") => Promise<void>; onAcceptBatch: (batch: Batch) => Promise<void>;
}) {
  const active = row.id === status.active_generation_id;
  const revisionChanged = row.source_revision !== status.content_revision;
  const stale = row.stale || revisionChanged;
  const canSwitch = !loading && Boolean(status.etag) && !active && !stale && row.status === "ready" && Boolean(row.manifest_hash)
    && (!row.integrity?.status || row.integrity.status === "passed");
  const action = row.activation_count ? "rollback" : "activate";
  const issues = [...(row.integrity?.blockers ?? []), ...(row.exclusions ?? [])];
  const distinct = new Map(issues.map(issue => [`${issue.version_id ?? issue.document_version_id ?? ""}:${issue.code ?? issue.reason ?? ""}`, issue]));
  const memberIssues = [
    row.integrity?.missing_child_refs?.length ? `缺少 ${row.integrity.missing_child_refs.length} 个成员` : "",
    row.integrity?.unexpected_child_refs?.length ? `${row.integrity.unexpected_child_refs.length} 个成员超出清单` : "",
    row.integrity?.invalid_child_refs?.length ? `${row.integrity.invalid_child_refs.length} 个成员无效` : "",
  ].filter(Boolean);
  return <section className="index-generation-detail" aria-label="所选索引代际">
    <h4>代际 {row.id.slice(0, 8)} · {generationStatus(row)}</h4>
    <p className="locator">{profileName}{row.kind !== "legacy" && ` · 配置修订 ${row.embedding_profile_id.slice(0, 8)}`}
      { ` · 来源修订 ${row.source_revision} · ${active ? "当前活动代际" : "待启用或历史代际"}` }</p>
    <p>成员 {row.actual_chunk_count ?? "待核对"} / {row.expected_chunk_count}</p>
    {!!memberIssues.length && <p className="warning">{memberIssues.join(" · ")}</p>}
    {row.integrity?.error_code && <p className="warning">{indexIssues[row.integrity.error_code] || "候选未通过完整性检查。"}</p>}
    {row.status === "failed" && !row.integrity?.error_code && !distinct.size && <p className="warning">构建未生成完整可用的代际。</p>}
    {!!distinct.size && <ul className="pdf-review-reasons">{[...distinct].map(([key, issue]) => <li key={key}>
      {[issue.version_id ?? issue.document_version_id, indexIssues[issue.code ?? issue.reason ?? ""] || "相关资料或索引成员仍需核对。"]
        .filter(Boolean).join(" · ")}
    </li>)}</ul>}
    {stale && <p className="muted">{revisionChanged ? "此代际的来源修订已过期，请基于当前来源构建。" : "此代际的来源或成员资格已变化，须重新核对。"}</p>}
    {row.kind !== "legacy" && !!row.embedding_batches?.length && <details className="index-batches">
      <summary>查看并受理嵌入批次（{row.batch_count ?? row.embedding_batches.length} 批）</summary>
      {row.status === "building" && !stale && <p className="locator">批次须有覆盖其内容的受控输入依据，受理后由后台处理。</p>}
      <ul>{row.embedding_batches.map(batch => <li key={batch.batch_no}>
        <span>第 {batch.batch_no} 批 · {batchStates[batch.state] || "状态待核对"}</span>
        {batch.error_code && <span className="locator">{indexIssues[batch.error_code] || "该批次尚有待处理问题。"} </span>}
        {batch.can_accept && row.status === "building" && !stale &&
          <button type="button" className="ghost" disabled={loading} onClick={() => void onAcceptBatch(batch)}>
            受理第 {batch.batch_no} 批
          </button>}
      </li>)}</ul>
    </details>}
    {canSwitch && <>
      <p className="muted">{activePointer(status)}。切换时将再次核对当前资料与索引成员。</p>
      <button type="button" onClick={() => void onSwitch(action)}>{action === "rollback" ? "回滚至此代际" : "启用此代际"}</button>
    </>}
  </section>;
}

function activePointer(status: IndexStatus) {
  return status.active_generation_id ? `当前活动代际为 ${status.active_generation_id.slice(0, 8)}` : "目前尚无活动代际";
}
