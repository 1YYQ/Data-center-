import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, etag, HttpError, id } from "../../api";
import { formValues, PendingOperations, Status, text, useOperation, Workspace } from "../../components";

const profilesPath = "/admin/service-profiles";
const selectionsPath = "/admin/service-selections";
const fixture = "service-synthetic-v1";
const modelPattern = String.raw`[A-Za-z0-9][A-Za-z0-9._:\/\-]{0,99}`;
const kinds = {
  generation: { label: "文本生成", adapter: "openai-chat-v1", models: ["synthetic-chat"] },
  embedding: { label: "文本向量", adapter: "bge-m3-v1", models: ["BAAI/bge-m3", "Pro/BAAI/bge-m3"] },
  parsing: { label: "文档解析", adapter: "mineru-v4-legacy-v1", models: ["vlm"] },
};
const testStates: Record<string, string> = {
  pending: "等待执行", running: "正在执行", awaiting_remote: "等待远端结果",
  succeeded: "测试通过", failed: "测试未通过", unknown: "结果未知",
};
const callStates: Record<string, string> = { dispatching: "等待响应", received: "收到响应", failed: "调用失败", unknown: "结果未知" };
const callPurposes: Record<string, string> = { api: "协议请求", upload: "合成样本上传", download: "测试结果下载" };
const actions = { activate: "启用", disable: "停用" };
const purposes = {
  "document.parse": { label: "文档解析", kind: "parsing" },
  "document.embed": { label: "文档向量化", kind: "embedding" },
  "query.answer": { label: "查询回答", kind: "generation" },
  "query.summary": { label: "查询摘要", kind: "generation" },
  "query.compaction": { label: "查询上下文压缩", kind: "generation" },
} as const;

type Action = keyof typeof actions;
type Revision = { id: string; endpoint_ref: string; adapter: string; model: string };
type TestRun = {
  id: string;
  revision_id: string;
  fixture_id: string;
  state: string;
  safe_error: string | null;
  valid_until: string | null;
  lease_until: string | null;
  checkpoint: { batch_id?: string };
};
type Call = {
  id: string;
  ordinal: number;
  method: string;
  purpose: string;
  state: string;
  response_status: number | null;
  safe_error: string | null;
};
type TestDetail = TestRun & { calls: Call[]; operator_can_resume: boolean };
type Profile = {
  id: string;
  kind: keyof typeof kinds;
  label: string;
  revision: number;
  etag?: string;
  enabled: boolean;
  active_revision_id: string | null;
  revisions: Revision[];
  tests: TestRun[];
  current_test_ids: string[];
  credential_state: "configured" | "not_configured";
  usage_and_budget: {
    max_requests_per_test: number;
    reported_usage: { total_tokens: number }[];
    settled_cost: null;
  };
};
type Listing = { items: Profile[]; endpoint_refs: string[] };
type Preview = { preview_id: string; allowed: boolean; action: Action; profile_etag: string; expires_at: string };
type Purpose = keyof typeof purposes;
type SelectionAction = "select" | "disable";
type Selection = {
  purpose: Purpose;
  revision: number;
  etag: string;
  enabled: boolean;
  revision_id: string | null;
  profile_id: string | null;
  test_run_id: string | null;
};
type SelectionListing = { items: Selection[]; purposes: Purpose[] };
type SelectionPreview = {
  id: string;
  purpose: Purpose;
  action: SelectionAction;
  revision_id: string | null;
  test_run_id: string | null;
  expires_at: string;
};
type Operation = ReturnType<typeof useOperation>;

const profilePath = (profileId: string) => `${profilesPath}/${id(profileId)}`;
const testsPath = (profileId: string, revisionId: string) => `${profilePath(profileId)}/revisions/${id(revisionId)}/test-runs`;
function utcTimestamp(value: string) {
  // SQLite stores the service timestamps as UTC without an offset.
  return Date.parse(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : value + "Z");
}
const formatTime = (value: string) => new Date(utcTimestamp(value)).toLocaleString("zh-CN");
const populated = (value: string, label: string) => {
  if (!value) throw Error(`请填写${label}。`);
  return value;
};

export function ServiceProfiles() {
  return <Workspace title="服务配置" eyebrow="SERVICE CONFIGURATION" intro="管理生成、向量与解析服务的合成自检和按用途选用。" role="system_admin">
    <ServiceWorkspace />
  </Workspace>;
}

function ServiceWorkspace() {
  const op = useOperation();
  const [listing, setListing] = useState<Listing | null>(null);
  const [selections, setSelections] = useState<SelectionListing | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [refreshCount, setRefreshCount] = useState(0);

  async function loadProfile(profileId: string) {
    const response = await api.request<Profile>(profilePath(profileId));
    setProfile({ ...response.data, etag: response.etag ?? response.data.etag });
  }

  async function loadListing() {
    const next = await api.get<Listing>(profilesPath);
    setListing(next);
    return next;
  }

  async function loadSelections() {
    setSelections(await api.get<SelectionListing>(selectionsPath));
  }

  async function refresh() {
    const capabilities = await api.get<{ permissions: string[] }>("/capabilities");
    if (!capabilities.permissions.includes("system.configure")) {
      setListing(null);
      setSelections(null);
      setProfile(null);
      throw Error("当前账号没有系统配置权限。");
    }
    const next = await loadListing();
    if (profile && next.items.some(row => row.id === profile.id)) await loadProfile(profile.id);
    else setProfile(null);
    setRefreshCount(count => count + 1);
    await loadSelections();
  }

  useEffect(() => { void op.perform(refresh); }, []);

  function createProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget, values = formValues(event);
    void op.perform(async () => {
      const created = await api.mutate<Profile>(profilesPath, {
        kind: text(values, "kind"), label: populated(text(values, "label"), "配置名称"),
      });
      await loadListing();
      await loadProfile(created.id);
      form.reset();
      form.closest("details")?.removeAttribute("open");
    }, "服务配置已创建。");
  }

  return <section className="service-workspace" aria-label="服务配置管理">
    <p className="service-scope"><strong>仅限本机合成数据</strong><span>企业数据准入未配置。</span></p>
    {op.message && <Status message={op.message} bad={op.bad} />}
    <fieldset className="operation-fields" disabled={op.busy} aria-busy={op.busy}>
      {listing && <PendingOperations onRecovered={refresh} perform={op.perform} />}
      {listing ? <div className="service-layout">
        <aside className="panel service-index">
          <div className="service-heading"><h2>配置列表</h2><button className="ghost" onClick={() => void op.perform(refresh, "服务配置已刷新。")}>刷新</button></div>
          {listing.items.length ? <div className="approval-list">{listing.items.map(row =>
            <button className="approval-item" key={row.id} aria-current={profile?.id === row.id} onClick={() => void op.perform(() => loadProfile(row.id))}>
              <span className="item-main"><strong className="item-subject">{row.label}</strong><span className={`state-pill ${row.enabled ? "state-verified" : ""}`}>{row.enabled ? "本地已启用" : "未启用"}</span></span>
              <span className="item-meta">{kinds[row.kind].label} · {row.revisions.length} 个修订</span>
            </button>,
          )}</div> : <p className="service-hint">尚无服务配置。</p>}
          <details className="service-new" open={!listing.items.length || undefined}>
            <summary>新建服务配置</summary>
            <form className="service-form" onSubmit={createProfile}>
              <label>服务类型<select name="kind">{Object.entries(kinds).map(([key, kind]) => <option key={key} value={key}>{kind.label}</option>)}</select></label>
              <label>配置名称<input name="label" required maxLength={120} /></label>
              <button type="submit">创建配置</button>
            </form>
          </details>
        </aside>
        <section className="panel service-sheet" aria-label="配置详情">
          {profile ? <ServiceDetail
            key={`${profile.id}-${refreshCount}`} profile={profile} endpointRefs={listing.endpoint_refs} operation={op}
            onChange={async () => { await loadListing(); await loadProfile(profile.id); }}
          /> : <div className="service-empty">
            <h2>{listing.items.length ? "选择一项服务配置" : "创建第一项服务配置"}</h2>
            <p className="muted">{listing.endpoint_refs.length ? "添加修订后，可运行固定合成样本并核对调用回执。" : "本机尚未登记合成测试端点。"}</p>
          </div>}
        </section>
      </div> : <section className="panel">
        {op.busy ? <p role="status">正在读取服务配置…</p> : <button className="ghost" onClick={() => void op.perform(refresh)}>重新读取配置</button>}
      </section>}
      {listing && <ServiceSelections profiles={listing.items} selections={selections} refreshCount={refreshCount} operation={op} onChange={loadSelections} />}
    </fieldset>
  </section>;
}

function revisionTitle(profile: Profile, revisionId: string) {
  const index = profile.revisions.findIndex(revision => revision.id === revisionId);
  const revision = profile.revisions[index];
  return revision ? `${profile.label} · 修订 ${index + 1} · ${revision.model} · ${revision.endpoint_ref}（${revision.id}）` : revisionId;
}

function ServiceSelections({ profiles, selections, refreshCount, operation, onChange }: {
  profiles: Profile[]; selections: SelectionListing | null; refreshCount: number; operation: Operation; onChange: () => Promise<void>;
}) {
  const [purpose, setPurpose] = useState<Purpose>("document.parse");
  const selection = selections?.items.find(row => row.purpose === purpose);

  return <section className="panel service-sheet service-selections" aria-labelledby="service-selections">
    <h2 id="service-selections">按用途选用服务</h2>
    <p className="service-hint">选用只适用于系统确认过的合成输入。启用配置并通过自检后，可在这里为各项用途指定修订。</p>
    {selections ? <>
      <label className="service-purpose">用途<select value={purpose} onChange={event => setPurpose(event.target.value as Purpose)}>
        {selections.purposes.map(value => <option key={value} value={value}>
          {purposes[value].label} · {selections.items.find(row => row.purpose === value)?.enabled ? "已选用" : "未选用"}
        </option>)}
      </select></label>
      {selection && <PurposeSelection key={`${purpose}-${selection.etag}-${refreshCount}`} selection={selection} profiles={profiles} operation={operation} onChange={onChange} />}
    </> : <button className="ghost" onClick={() => void operation.perform(onChange)}>读取用途状态</button>}
  </section>;
}

function PurposeSelection({ selection, profiles, operation, onChange }: {
  selection: Selection; profiles: Profile[]; operation: Operation; onChange: () => Promise<void>;
}) {
  const [action, setAction] = useState<SelectionAction>("select");
  const [revisionId, setRevisionId] = useState("");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<SelectionPreview | null>(null);
  const [expired, setExpired] = useState(false);
  const prefix = `${selectionsPath}/${id(selection.purpose)}`;
  const sourceProfile = profiles.find(profile => profile.id === selection.profile_id);
  const candidates = profiles.flatMap(profile => {
    const activeId = profile.active_revision_id;
    if (profile.kind !== purposes[selection.purpose].kind || !profile.enabled || !activeId) return [];
    const latest = profile.tests.filter(test => test.revision_id === activeId && profile.current_test_ids.includes(test.id) &&
      !!test.valid_until && utcTimestamp(test.valid_until) > Date.now())
      .sort((a, b) => utcTimestamp(b.valid_until!) - utcTimestamp(a.valid_until!))[0];
    if (!latest || (selection.enabled && activeId === selection.revision_id && latest.id === selection.test_run_id)) return [];
    return [{ id: activeId, title: revisionTitle(profile, activeId) }];
  });
  const candidate = candidates.find(value => value.id === revisionId);
  const pending = api.pendingCommands().some(command => [prefix + "/previews", `${prefix}/${action}`].includes(command.path));
  const canPreview = !pending && (action === "select" ? !!candidate : selection.enabled);
  const previewCurrent = preview && !expired && canPreview && preview.purpose === selection.purpose &&
    preview.action === action && (action === "disable" || preview.revision_id === revisionId);

  useEffect(() => {
    if (!preview) return;
    const timer = window.setTimeout(() => setExpired(true), Math.max(0, utcTimestamp(preview.expires_at) - Date.now()));
    return () => window.clearTimeout(timer);
  }, [preview]);

  function clearPreview() {
    setPreview(null);
    setExpired(false);
  }

  function previewChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canPreview) return;
    clearPreview();
    void operation.perform(async () => {
      try {
        setPreview(await api.mutate<SelectionPreview>(prefix + "/previews", {
          action, ...(candidate && action === "select" && { revision_id: candidate.id }), reason: populated(reason.trim(), "选用变更理由"),
        }, etag(selection)));
      } catch (error) {
        if (error instanceof HttpError && error.status === 412) await onChange();
        throw error;
      }
    });
  }

  function applyChange() {
    if (!previewCurrent) return;
    if (utcTimestamp(preview.expires_at) <= Date.now()) {
      setExpired(true);
      return;
    }
    void operation.perform(async () => {
      try {
        await api.mutate<Selection>(`${prefix}/${action}`, { preview_id: preview.id, reason: reason.trim() }, etag(selection));
      } catch (error) {
        if (error instanceof HttpError && [409, 412].includes(error.status)) {
          clearPreview();
          await onChange();
        }
        throw error;
      }
      clearPreview();
      setReason("");
      setRevisionId("");
      await onChange();
    }, action === "select" ? "此用途的合成服务已选用。" : "此用途的合成服务已停用。");
  }

  return <>
    <dl className="service-facts service-revision-facts service-selection-facts">
      <div><dt>选用状态</dt><dd>{selection.enabled ? "合成用途已选用" : "未选用"}</dd></div>
      <div><dt>当前修订</dt><dd>{selection.enabled && selection.revision_id
        ? (sourceProfile ? revisionTitle(sourceProfile, selection.revision_id) : selection.revision_id)
        : "—"}</dd></div>
      {selection.enabled && selection.test_run_id && <div><dt>绑定自检</dt><dd>{selection.test_run_id}</dd></div>}
    </dl>
    <form className="service-form" onSubmit={previewChange}>
      <label>选用变更<select value={action} onChange={event => { setAction(event.target.value as SelectionAction); clearPreview(); }}>
        <option value="select">选用配置修订</option><option value="disable" disabled={!selection.enabled}>停用此用途</option>
      </select></label>
      {action === "select" && (candidates.length ? <label>目标配置修订<select required value={candidate?.id ?? ""} onChange={event => { setRevisionId(event.target.value); clearPreview(); }}>
        <option value="" disabled>请选择修订</option>
        {candidates.map(value => <option key={value.id} value={value.id}>{value.title}</option>)}
      </select></label> : <p className="service-hint">暂无可选修订。需要已启用的同类型修订和有效的合成自检。</p>)}
      <label>选用变更理由<textarea required maxLength={500} rows={2} value={reason} onChange={event => { setReason(event.target.value); clearPreview(); }} /></label>
      <button type="submit" disabled={!canPreview}>预检用途变更</button>
      {pending && <p className="service-hint">请先从页面上方恢复此用途的原操作回执。</p>}
      {previewCurrent && <div className="service-preview" aria-label="用途变更预检结果">
        <p role="status"><strong>预检通过</strong> · 有效至 {formatTime(preview.expires_at)}</p>
        <p>{purposes[selection.purpose].label} · {action === "select" ? candidate?.title : "停用"}</p>
        {preview.test_run_id && <p>绑定自检 <code>{preview.test_run_id}</code></p>}
        <p>{action === "select" ? "新任务使用所选修订及其自检记录，已有任务保留原绑定。" : "停用将阻止该用途的新调用，也会阻止原绑定下尚未发送的调用。重新选用后，原绑定仍然失效。"}</p>
        <button type="button" className={action === "disable" ? "danger" : undefined} onClick={applyChange}>{action === "select" ? "确认选用" : "确认停用用途"}</button>
      </div>}
      {expired && <p className="service-hint" role="status">预检已过期，请重新预检。</p>}
    </form>
  </>;
}

function ServiceDetail({ profile, endpointRefs, operation, onChange }: {
  profile: Profile; endpointRefs: string[]; operation: Operation; onChange: () => Promise<void>;
}) {
  const kind = kinds[profile.kind];
  const prefix = profilePath(profile.id);
  const [revisionId, setRevisionId] = useState(profile.active_revision_id ?? profile.revisions.at(-1)?.id ?? "");
  const [testDetail, setTestDetail] = useState<TestDetail | null>(null);
  const [action, setAction] = useState<Action>("activate");
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewExpired, setPreviewExpired] = useState(false);

  const revision = profile.revisions.find(item => item.id === revisionId);
  const activeNumber = profile.revisions.findIndex(item => item.id === profile.active_revision_id) + 1;
  const tests = profile.tests.filter(test => test.revision_id === revision?.id);
  const currentTest = (test: TestRun) => profile.current_test_ids.includes(test.id) && !!test.valid_until && utcTimestamp(test.valid_until) > Date.now();
  const testReady = tests.some(currentTest);
  const canActivate = testReady && !!revision && (!profile.enabled || revision.id !== profile.active_revision_id);
  const canPreview = action === "activate" ? canActivate : profile.enabled;
  const revisionRegistered = !!revision && endpointRefs.includes(revision.endpoint_ref);
  const previewCurrent = preview && preview.allowed && preview.action === action && preview.profile_etag === etag(profile) && !previewExpired;
  const budget = profile.usage_and_budget;
  const reportedTokens = budget.reported_usage.reduce((sum, value) => sum + value.total_tokens, 0);

  useEffect(() => {
    if (!preview) return;
    const timer = window.setTimeout(() => setPreviewExpired(true), Math.max(0, utcTimestamp(preview.expires_at) - Date.now()));
    return () => window.clearTimeout(timer);
  }, [preview]);

  function clearPreview() {
    setPreview(null);
    setPreviewExpired(false);
  }

  function chooseRevision(value: string) {
    clearPreview();
    setRevisionId(value);
    setTestDetail(null);
  }

  async function readTest(test: TestRun) {
    setTestDetail(await api.get<TestDetail>(`${testsPath(profile.id, test.revision_id)}/${id(test.id)}`));
  }

  function addRevision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget, values = formValues(event);
    clearPreview();
    void operation.perform(async () => {
      const created = await api.mutate<Revision>(prefix + "/revisions", {
        endpoint_ref: text(values, "endpoint_ref"), adapter: kind.adapter, model: populated(text(values, "model"), "模型"),
      }, etag(profile));
      await onChange();
      chooseRevision(created.id);
      form.closest("details")?.removeAttribute("open");
    }, "新修订已保存。");
  }

  function runTest() {
    if (!revision || !revisionRegistered) return;
    clearPreview();
    void operation.perform(async () => {
      const test = await api.mutate<TestRun>(testsPath(profile.id, revision.id), { fixture_id: fixture });
      await onChange();
      await readTest(test);
    }, "自检回执已读取。");
  }

  function resumeTest(test: TestDetail, values: FormData) {
    if (!test.operator_can_resume || !canResume(test)) return;
    clearPreview();
    void operation.perform(async () => {
      await api.mutate(`${testsPath(profile.id, test.revision_id)}/${id(test.id)}/resume`, { reason: populated(text(values, "resume_reason"), "核对理由") }, etag(profile));
      await onChange();
      await readTest(test);
    }, "原自检的状态已更新。");
  }

  function previewChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canPreview) return;
    clearPreview();
    void operation.perform(async () => {
      setPreview(await api.mutate<Preview>(prefix + "/change-preview", {
        action, ...(action === "activate" && { revision_id: revisionId }), reason: populated(reason.trim(), "变更理由"),
      }, etag(profile)));
    });
  }

  function applyChange() {
    if (!previewCurrent) return;
    if (utcTimestamp(preview.expires_at) <= Date.now()) {
      setPreviewExpired(true);
      return;
    }
    void operation.perform(async () => {
      try {
        await api.mutate(`${prefix}/${action}`, { preview_id: preview.preview_id, reason: populated(reason.trim(), "变更理由") }, etag(profile));
      } catch (error) {
        if (error instanceof HttpError && [409, 412].includes(error.status)) clearPreview();
        throw error;
      }
      clearPreview();
      setReason("");
      await onChange();
    }, action === "activate" ? "合成配置已启用。" : "合成配置已停用。");
  }

  return <>
    <header className="service-record">
      <p className="muted">{kind.label}</p><h2>{profile.label}</h2>
      <dl className="service-facts">
        <div><dt>配置</dt><dd>{profile.revisions.length ? "已登记修订" : "待添加修订"}</dd></div>
        <div><dt>启停</dt><dd>{profile.enabled ? `本地已启用修订 ${activeNumber}` : "未启用"}</dd></div>
        <div><dt>本机凭据</dt><dd>{profile.credential_state === "configured" ? "已配置" : "未配置"}</dd></div>
        <div><dt>企业数据准入</dt><dd>未配置</dd></div>
        <div><dt>健康状态</dt><dd>未知</dd></div>
      </dl>
    </header>
    <section className="service-section" aria-labelledby="service-revisions">
      <h3 id="service-revisions">配置修订</h3>
      {revision && <div className="service-revision">
        <label>当前查看修订<select value={revision.id} onChange={event => chooseRevision(event.target.value)}>
          {profile.revisions.map((item, index) => <option key={item.id} value={item.id}>
            修订 {index + 1} · {item.model} · {item.endpoint_ref}{profile.active_revision_id === item.id && (profile.enabled ? "（已启用）" : "（曾启用）")}
          </option>)}
        </select></label>
        <dl className="service-facts service-revision-facts">
          <div><dt>端点引用</dt><dd>{revision.endpoint_ref}</dd></div>
          <div><dt>适配器</dt><dd>{revision.adapter}</dd></div>
          <div><dt>模型</dt><dd>{revision.model}</dd></div>
        </dl>
      </div>}
      {!endpointRefs.length ? <p className="service-hint">本机尚未登记合成测试端点。</p> : <details className="service-new" open={!revision || undefined}>
        <summary>添加修订</summary>
        <form className="service-form service-form-grid" onSubmit={addRevision}>
          <label>端点引用<select name="endpoint_ref" required>{endpointRefs.map(ref => <option key={ref} value={ref}>{ref}</option>)}</select></label>
          <label>模型{profile.kind === "generation"
            ? <input name="model" required maxLength={100} pattern={modelPattern} defaultValue={kind.models[0]} aria-describedby="service-model-hint" />
            : <select name="model">{kind.models.map(model => <option key={model} value={model}>{model}</option>)}</select>}
          </label>
          <p className="service-hint" id="service-model-hint">适配器 <code>{kind.adapter}</code>。模型须获所选端点允许。</p>
          <button type="submit">保存修订</button>
        </form>
      </details>}
    </section>
    {revision && <section className="service-section" aria-labelledby="service-tests">
      <div className="service-heading"><h3 id="service-tests">合成自检</h3><button className="ghost" disabled={!revisionRegistered} onClick={runTest}>运行合成自检</button></div>
      <p className="service-hint">固定样本 <code>{fixture}</code> · {testReady ? "当前修订已有有效通过记录" : "当前修订无有效通过记录"}</p>
      <details className="service-usage">
        <summary>自检用量和费用</summary>
        <dl className="service-facts service-revision-facts">
          <div><dt>单次自检请求上限</dt><dd>{budget.max_requests_per_test} 次</dd></div>
          <div><dt>该配置的服务报告用量</dt><dd>{budget.reported_usage.length ? `${reportedTokens.toLocaleString("zh-CN")} tokens（${budget.reported_usage.length} 次报告）` : "未报告"}</dd></div>
          <div><dt>实际费用</dt><dd>未知</dd></div>
        </dl>
      </details>
      {!revisionRegistered && endpointRefs.length > 0 && <p className="service-hint">此修订的端点已从本机登记中移除。</p>}
      {!tests.length && <p className="service-hint">尚无自检记录。</p>}
      <ul className="service-test-list">{tests.map(test => <li key={test.id}>
        <div>
          <span className={`state-pill ${currentTest(test) ? "state-verified" : test.state === "failed" ? "state-rejected" : ""}`}>{testStates[test.state] || "未知状态"}</span>
          <p className="locator">{test.id}</p>
          {test.safe_error && <p className="locator">错误代码 <code>{test.safe_error}</code></p>}
        </div>
        <button className="ghost" onClick={() => void operation.perform(() => readTest(test))}>查看测试回执</button>
      </li>)}</ul>
      {testDetail && <TestReceipt key={testDetail.id} test={testDetail} onResume={values => resumeTest(testDetail, values)} />}
    </section>}
    <section className="service-section" aria-labelledby="service-enable">
      <h3 id="service-enable">启停配置</h3>
      <form className="service-form" onSubmit={previewChange}>
        <label>变更操作<select value={action} onChange={event => { setAction(event.target.value as Action); clearPreview(); }}>
          <option value="activate">启用所选修订</option><option value="disable">停用服务配置</option>
        </select></label>
        {action === "activate" && !canActivate && <p className="service-hint">{profile.enabled && profile.active_revision_id === revisionId ? "所选修订已经启用。" : "启用前需要所选修订的有效合成自检。"}</p>}
        {action === "disable" && !profile.enabled && <p className="service-hint">此服务配置当前未启用。</p>}
        <label>变更理由<textarea value={reason} onChange={event => { setReason(event.target.value); clearPreview(); }} rows={2} required maxLength={500} /></label>
        <button type="submit" disabled={!canPreview}>预检{actions[action]}</button>
        {previewCurrent && <div className="service-preview" aria-label="变更预检结果">
          <p role="status"><strong>预检通过</strong> · 有效至 {formatTime(preview.expires_at)}</p>
          <p>{action === "activate" ? "将启用所选修订用于本机合成测试。" : "将停用此配置。"}</p>
          <button type="button" className={action === "disable" ? "danger" : undefined} onClick={applyChange}>确认{actions[action]}</button>
        </div>}
        {previewExpired && <p className="service-hint" role="status">预检已过期，请重新预检。</p>}
      </form>
    </section>
  </>;
}

function canResume(test: TestRun, now = Date.now()) {
  if (test.state === "pending") return true;
  if (test.state === "running") return !test.lease_until || utcTimestamp(test.lease_until) <= now;
  return !!test.checkpoint.batch_id && ["awaiting_remote", "unknown"].includes(test.state);
}

function TestReceipt({ test, onResume }: { test: TestDetail; onResume: (values: FormData) => void }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (test.state !== "running" || !test.lease_until) return;
    const timer = window.setTimeout(() => setNow(Date.now()), Math.max(0, utcTimestamp(test.lease_until) - Date.now()));
    return () => window.clearTimeout(timer);
  }, [test.state, test.lease_until]);

  return <section className="service-receipt" aria-label="自检回执">
    <h4>自检回执 · {testStates[test.state] || "未知状态"}</h4>
    <p className="locator">{test.id}</p>
    {test.valid_until && <p className="service-hint">通过记录有效至 {formatTime(test.valid_until)}</p>}
    {test.safe_error && <p className="service-hint">错误代码 <code>{test.safe_error}</code></p>}
    {test.checkpoint.batch_id && <p className="locator">远端批次 {test.checkpoint.batch_id}</p>}
    <h4>调用回执</h4>
    {test.calls.length ? <ol className="service-calls">{test.calls.map(call => <li key={call.id}>
      <strong>{callPurposes[call.purpose] || "服务调用"} · {call.method}</strong>
      <span>{callStates[call.state] || "未知状态"}{call.response_status !== null && ` · HTTP ${call.response_status}`}</span>
      {call.safe_error && <code>{call.safe_error}</code>}
    </li>)}</ol> : <p className="service-hint">目前没有调用记录。</p>}
    {canResume(test, now) && (test.operator_can_resume ? <form className="service-form" onSubmit={event => { event.preventDefault(); onResume(formValues(event)); }}>
      <label>核对理由<textarea name="resume_reason" rows={2} required maxLength={500} /></label>
      <button className="ghost" type="submit">{test.state === "pending" ? "继续未执行的自检" : test.checkpoint.batch_id ? "核对远端结果" : "核对执行状态"}</button>
    </form> : <p className="service-hint">仅原自检发起人可继续核对此次自检。</p>)}
    {test.state === "unknown" && !test.checkpoint.batch_id && <p className="service-hint">缺少远端回执标识，无法继续核对此次自检。</p>}
  </section>;
}
