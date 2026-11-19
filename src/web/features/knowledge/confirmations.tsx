import { useState } from "react";
import { api, etag, id, stableJSON } from "../../api";
import { Field, formValues, selectedValues, text } from "../../components";
import type { Block, Confirmation, ConfirmationOptions, ObservationDisposition, ParseBlocks, VerificationOptions } from "./types";
import { blockPage } from "./types";
import { blockName, OriginalLink } from "./BlockReview";
import { kindName } from "./types";
import { stateLabel } from "../../labels";
type Perform = (action: () => Promise<void>, success?: string) => Promise<void>;
type Props = { base: string; onChange: () => Promise<void>; perform: Perform };
const conclusions: Record<string, [string, string][]> = { applicability: [["applicable", "适用"], ["not_applicable", "不适用"], ["conflict", "存在冲突"]], key_content: [["accepted", "关键内容已确认"], ["rejected", "不接受"]], classification: [["classified", "分类已确认"], ["conflict", "存在冲突"]] };

export function ConfirmationForm({ options, base, run, blocks, onChange, perform, preferKeyContent }: Props & {
  options: ConfirmationOptions; run: string; blocks: ParseBlocks | null; preferKeyContent: boolean;
}) {
  const initialKind = preferKeyContent ? "key_content" : "applicability";
  const [kind, setKind] = useState(initialKind);
  const [authorityId, setAuthorityId] = useState("");
  const [conclusion, setConclusion] = useState(conclusions[initialKind][0][0]);
  const available = options.authority_bases.filter(row => row.purposes.includes(kind));
  const authority = available.find(row => row.id === authorityId) || available[0];
  const keyContent = kind === "key_content";
  const sourceScope = keyContent ? options.run_source_bindings?.[run] : undefined;
  const modern = Boolean(blocks?.ir_schema_version || sourceScope);
  const verificationReady = Boolean(sourceScope?.source_qc_ref && blocks?.source_qc?.current);
  const observations = blocks?.items.filter(block => Boolean(sourceScope?.observation_bindings[block.id])) ?? [];
  const leaves = blocks?.items.filter(block => Boolean(sourceScope?.block_bindings[block.id])) ?? [];
  const canSubmit = Boolean(authority) && (!keyContent || (run && blocks && (!modern || verificationReady)));

  return <>
    <p className="warning">这里只登记系统外真实确认的事实。请关联受控依据，再由有职责的人员完成核查。</p>
    <form onSubmit={event => {
      event.preventDefault();
      const values = formValues(event);
      void perform(async () => {
        if (!authority || !canSubmit) throw Error("请先核对当前批次和已登记职责依据。");
        let scope: Record<string, unknown> = { ...options.source_binding };
        if (kind === "classification") {
          scope = { ...scope, access_level: text(values, "level"), category: text(values, "category"), need_class: text(values, "need"), rule_version: text(values, "scope-reason") };
        }
        if (keyContent && sourceScope) {
          if (!values.has("actual_source_scope")) throw Error("请确认实际业务依据覆盖所示版本、原页、内容和关系。");
          scope = { ...scope, ...sourceScope, observation_dispositions: conclusion === "accepted" ? observationDispositions(values, observations) : {} };
          const repairRefs = values.getAll("repair_of_hold_refs").map(String);
          if (conclusion === "accepted" && repairRefs.length) scope.repair_of_hold_refs = repairRefs;
        }
        if (keyContent && !modern) {
          const bindings = options.run_block_bindings?.[run] ?? Object.fromEntries((blocks?.items ?? []).map(row => [row.id, row.content_hash]));
          scope = { ...scope, parse_run_id: run, block_bindings: bindings };
        }
        await api.mutate(base + "/business-confirmations", { kind, conclusion, scope,
          actual_confirmer: { ...authority.identity, identity_label: text(values, "confirmer"), responsibility: text(values, "responsibility"), authority_basis_ref: authority.id },
          confirmed_at: new Date(text(values, "confirmed")).toISOString(), proof_refs: [] });
        await onChange();
      }, "真实确认事实已登记，等待依据关联与核查。");
    }}>
      <div className="form-grid">
        <label>确认类型<select id="knowledge-confirmation-kind" value={kind} onChange={event => {
          const type = event.target.value;
          setKind(type); setAuthorityId(""); setConclusion(conclusions[type][0][0]);
        }}>{Object.entries(kindName).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label>
        <label>真实结论<select name="conclusion" value={conclusion} onChange={event => setConclusion(event.target.value)}>
          {conclusions[kind].map(([key, value]) => <option key={key} value={key}>{value}</option>)}
        </select></label>
        <Field key={`person-${authority?.id}`} label="实际确认人" name="confirmer" value={authority?.identity_label || ""} />
        <Field key={`responsibility-${authority?.id}`} label="确认职责" name="responsibility" value={authority?.responsibility || ""} />
        <label>已登记职责依据<select value={authority?.id || ""} onChange={event => setAuthorityId(event.target.value)} required>
          {!available.length && <option value="">没有匹配的当前职责依据</option>}
          {available.map(row => <option key={row.id} value={row.id}>{row.identity_label} · {row.responsibility}</option>)}
        </select></label>
        <Field label="确认时间" name="confirmed" type="datetime-local" />
        {kind === "classification" && <>
          <Field label="范围规则版本" name="scope-reason" />
          <Field label="访问等级" name="level" value={options.classification.access_level || ""} />
          <Field label="类别" name="category" value={options.classification.category || ""} />
          <Field label="用途分类" name="need" value={options.classification.need_class || ""} />
        </>}
      </div>
      {keyContent && (!run || !blocks) && <p className="muted">请先在「原文核验」选择并读取解析批次。</p>}
      {keyContent && modern && !verificationReady && <p className="warning">此批次的源质量检查尚未通过或已失效，无法登记当前来源的专业确认。</p>}
      {keyContent && sourceScope && <section className="pdf-review-section" aria-labelledby="pdf-confirm-scope">
        <h3 id="pdf-confirm-scope">确认范围</h3>
        <p className="muted">{Object.keys(sourceScope.page_bindings).length} 个原页 · {Object.keys(sourceScope.block_bindings).length} 个原文内容块 · {Object.keys(sourceScope.container_bindings).length} 个结构容器 · {Object.keys(sourceScope.relation_bindings).length} 条适用关系</p>
        <label className="toggle"><input type="checkbox" name="actual_source_scope" required />实际确认人的依据覆盖此批次的原页、内容及适用关系</label>
        {conclusion === "accepted" && !!observations.length && <>
          <h4>原页清点项的实际处置</h4>
          <p className="muted">只登记有实际依据的处置，其余保持待核验。</p>
          <div className="stack">{observations.map(block => <ObservationField key={block.id} block={block} leaves={leaves} base={base} />)}</div>
        </>}
        {conclusion === "accepted" && Boolean(options.quality_holds?.length) && <div className="space">
          <label>本次实际核验涉及的质量暂停<select name="repair_of_hold_refs" multiple size={4}>
            {options.quality_holds?.map(hold => <option key={hold.id} value={hold.id}>
              {hold.reason} · 修订 {hold.revision} · {hold.id.slice(0, 8)}
            </option>)}
          </select></label>
          <p className="muted">仅选择实际确认人的修复依据已经覆盖的暂停记录。默认全部未选。</p>
          <details><summary>查看各暂停记录的影响范围</summary>
            <ul className="pdf-review-reasons">{options.quality_holds?.map(hold => <li key={hold.id}>
              {hold.reason} · {hold.id}<p>受影响的来源 {hold.affected_refs.join("、") || "待核对"}</p>
            </li>)}</ul>
          </details>
        </div>}
      </section>}
      <button className="space" disabled={!canSubmit}>登记真实确认</button>
    </form>
  </>;
}

function ObservationField({ block, base, leaves }: { block: Block; base: string; leaves: Block[] }) {
  const [decision, setDecision] = useState("");
  const page = blockPage(block);
  const samePage = leaves.filter(leaf => blockPage(leaf) === page);
  const otherPages = leaves.filter(leaf => blockPage(leaf) !== page);
  const field = "observation-" + block.id;
  return <fieldset className="observation-field">
    <legend>{blockName(block)}</legend>
    {page && <OriginalLink base={base} page={page} />}
    <label>处置结论<select name={field} value={decision} onChange={event => setDecision(event.target.value)}>
      <option value="">待核验</option>
      <option value="covered_by">由其他原文内容完整覆盖</option>
      <option value="decorative">装饰内容</option>
      <option value="out_of_scope">超出已确认的业务范围</option>
      <option value="image_only_accepted">实际政策允许只保留原图</option>
    </select></label>
    {decision === "covered_by" && <label>覆盖此项的原文内容块<select name={field + "-blocks"} multiple required size={4}>
      {samePage.map(leaf => <option key={leaf.id} value={leaf.id}>{blockName(leaf)} · {leaf.text.slice(0, 52)}</option>)}
      {otherPages.length > 0 && <optgroup label="其他原页">{otherPages.map(leaf =>
        <option key={leaf.id} value={leaf.id}>{blockName(leaf)} · {leaf.text.slice(0, 52)}</option>)}</optgroup>}
    </select></label>}
    {decision === "image_only_accepted" && <Field label="实际政策或人工批准依据" name={field + "-policy"} />}
    {decision && <Field key={decision} label="实际处置依据说明" name={field + "-reason"} />}
  </fieldset>;
}

function observationDispositions(values: FormData, observations: Block[]) {
  const dispositions: Record<string, ObservationDisposition> = {};
  for (const block of observations) {
    const field = "observation-" + block.id;
    const decision = text(values, field);
    if (!decision) continue;
    const reason = text(values, field + "-reason");
    if (!reason) throw Error(`${blockName(block)}缺少实际处置依据说明。`);
    if (decision === "covered_by") {
      const block_refs = values.getAll(field + "-blocks").map(String);
      if (!block_refs.length) throw Error(`${blockName(block)}缺少覆盖此项的原文内容块。`);
      dispositions[block.id] = { decision, reason, block_refs };
    } else if (decision === "image_only_accepted") {
      const policy_ref = text(values, field + "-policy");
      if (!policy_ref) throw Error(`${blockName(block)}缺少实际政策或人工批准依据。`);
      dispositions[block.id] = { decision, reason, policy_ref };
    } else if (decision === "decorative" || decision === "out_of_scope") {
      dispositions[block.id] = { decision, reason };
    }
  }
  return dispositions;
}

export function EvidenceUpload({ base, perform, onChange }: Props) {
  return <details className="space"><summary>补充本版本真实依据材料</summary><p className="muted">附件只作为待核查材料保存，不会自动证明业务结论。</p><form className="stack" onSubmit={event => { event.preventDefault(); const values = formValues(event), form = event.currentTarget; void perform(async () => {
    const file = values.get("file"); if (!(file instanceof File) || !file.size) throw Error("请选择材料文件。");
    await api.upload(base + "/confirmation-evidence", file, { source_description: text(values, "source") }); form.reset(); await onChange();
  }, "待核查材料已保存；须在对应确认中关联并核查。"); }}><Field label="依据来源说明" name="source" /><label>材料文件<input name="file" type="file" accept="application/pdf,.pdf" required /></label><button className="ghost">上传待核查材料</button></form></details>;
}
export function ConfirmationRecord({ row, base, perform, onChange, currentOptions, superseded }: Props & {
  row: Confirmation; currentOptions: ConfirmationOptions | null; superseded: boolean;
}) {
  const [options, setOptions] = useState<VerificationOptions | null>(null);
  const [refs, setRefs] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const editable = row.verification_state === "pending" && !superseded;
  const prefix = base + "/business-confirmations/" + id(row.id);
  const verifiedEvidence = options && refs.length > 0
    && refs.every(ref => options.evidence.some(item => item.id === ref && item.attested && options.proof_refs.includes(ref)));
  const state = row.current ? "当前有效" : row.verification_state === "pending" && !superseded ? "待核查" : "当前不可用";
  const staleReason = row.current ? null : confirmationStateReason(row, currentOptions, superseded);
  return <article className="record" data-confirmation-id={row.id}>
    <strong>{kindName[row.kind] || row.kind} · {stateLabel(row.conclusion)} · {state}</strong>
    <p>确认人 {row.actual_confirmer?.identity_label || "未知"} · {row.confirmed_at || "时间未知"}</p>
    {staleReason && <p className="muted">{staleReason}</p>}
    <p className="locator">记录 {row.id} · 依据 {row.proof_refs.join("、") || "未关联"}</p>
    <button className="ghost" onClick={() => void perform(async () => {
      const result = await api.get<VerificationOptions>(prefix + "/verification-options");
      setOptions(result); setRefs(result.proof_refs);
    })}>查看依据与核查</button>
    {options && <div className="confirmation-detail">
      <label>本版本依据材料（须逐项核对）<select multiple size={3} value={refs} disabled={!editable} onChange={event => setRefs(selectedValues(event.target))}>
        {options.evidence.map(item => <option key={item.id} value={item.id}>
          {item.source_description || "来源未知"} · {item.attested ? "已有受控核实" : "尚未受控核实"}
        </option>)}
      </select></label>
      {options.evidence.filter(item => item.has_file).map(item =>
        <a key={item.id} href={base + "/confirmation-evidence/" + id(item.id) + "/file"} target="_blank" rel="noopener noreferrer">查看依据 {item.id}</a>)}
      <label>核查原因<input value={reason} onChange={event => setReason(event.target.value)} disabled={!editable} /></label>
      <button className="ghost" disabled={!editable} onClick={() => void perform(async () => {
        await api.mutate(prefix + "/evidence-refs", { proof_refs: refs }, etag({ revision: options.revision }), "PATCH");
        await onChange();
      }, "已保存依据关联，等待受控核查。")}>保存依据关联</button>
      {!verifiedEvidence && <p className="warning">材料须先关联并经受控核实。</p>}
      <button disabled={!editable || !verifiedEvidence || !reason.trim()} onClick={() => void perform(async () => {
        await api.mutate(prefix + "/verification", { decision: "verified", reason, authority_basis_ref: row.actual_confirmer?.authority_basis_ref,
          checked_proof_refs: refs }, etag({ revision: options.revision }));
        await onChange();
      }, "系统核查结果已读取。")}>记录核查通过</button>
    </div>}
  </article>;
}

function confirmationStateReason(row: Confirmation, options: ConfirmationOptions | null, superseded: boolean) {
  if (superseded) return "已有记录明确替代此确认。";
  if (row.verification_state === "pending") return row.proof_refs.length ? "等待完成受控核查。" : "等待关联受控依据。";
  if (row.verification_state === "rejected") return "此记录的系统核查已退回。";
  if (!options) return "当前依据有效性待重新核对。";
  if (Object.entries(options.source_binding).some(([key, value]) => stableJSON(row.scope[key]) !== stableJSON(value))) {
    return "此记录的原件或业务适用范围与当前版本有差异。";
  }
  if (row.kind === "key_content" && row.scope.parse_run_id && row.scope.ir_artifact_id) {
    const current = options.run_source_bindings?.[row.scope.parse_run_id];
    if (!current || current.ir_artifact_id !== row.scope.ir_artifact_id || current.source_qc_ref !== row.scope.source_qc_ref
        || current.source_projection_hash !== row.scope.source_projection_hash) return "此记录的解析来源、原页映射或技术检查已变化。";
  }
  if (!options.authority_bases.some(authority => authority.id === row.actual_confirmer?.authority_basis_ref)) {
    return "登记时使用的职责依据未出现在本版本当前可用职责中。";
  }
  return "关联材料或核查依赖的当前有效性待重新核对。";
}
