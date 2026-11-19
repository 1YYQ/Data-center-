import { useEffect, useState } from "react";
import { api, etag, id } from "../api";
import { formValues, Status, text, useOperation } from "../components";

type Origin = { task_id: string } | { document_version_id: string };
type Feedback = { id: string; status: string; text?: string; reply?: string; update_available?: boolean };
type Assignment = Feedback & { assignment: "unclaimed" | "mine" | "assigned"; revision: number; sources: Manual[] };
type Manual = { title: string; revision_label: string; file_url: string; reference_only: boolean };
type Page<T> = { items: T[]; next_cursor: string | null };
const labels: Record<string, string> = {
  awaiting_routing: "等待受理", submitted: "待领取", processing: "处理中",
  replied: "已答复", access_limited: "资料权限已变化",
};

export function FeedbackPanel({ origin, onQuery }: { origin: Origin; onQuery: (taskId: string) => void | Promise<void> }) {
  const manualOrigin = "document_version_id" in origin;
  const source = new URLSearchParams(origin).toString();
  const op = useOperation();
  const [rows, setRows] = useState<Feedback[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [manual, setManual] = useState<Manual | null>(null);
  const [ready, setReady] = useState(false);

  async function refresh(next?: string) {
    if (!next) { setRows([]); setManual(null); setReady(false); }
    const result = await api.get<Page<Feedback> & { manual: Manual | null }>(
      "/document-feedback?" + source + (next ? "&cursor=" + id(next) : ""));
    setRows(previous => next ? [...previous, ...result.items] : result.items);
    setCursor(result.next_cursor);
    setManual(result.manual);
    setReady(true);
  }

  useEffect(() => { void op.perform(() => refresh()); }, [source]);
  return <section className="stack" aria-label="资料问题反馈">
    {manual && <div><h2>{manual.title} · {manual.revision_label}</h2>
      {manual.reference_only && <p className="warning">仅供参考</p>}
      <a href={manual.file_url} target="_blank" rel="noopener noreferrer">查看本版本原文 PDF</a>
    </div>}
    <div className="section-heading"><h3>资料问题反馈</h3>
      <button type="button" className="ghost" disabled={op.busy} onClick={() => void op.perform(() => refresh())}>刷新反馈</button>
    </div>
    {ready && <form className="stack" onSubmit={event => {
      event.preventDefault();
      const form = event.currentTarget, note = text(formValues(event), "feedback");
      void op.perform(async () => {
        await api.mutate("/document-feedback", { ...origin, text: note }, undefined, "POST", false);
        form.reset();
        await refresh();
      }, "反馈已保存，可在这里查看进度。");
    }}>
      <label>遇到的资料问题<textarea name="feedback" required maxLength={2000} rows={2} /></label>
      <button disabled={op.busy}>提交资料反馈</button>
    </form>}
    {rows.map(row => <article className="evidence" key={row.id}>
      <p className="muted">{labels[row.status]}</p>
      {row.text && <p>{row.text}</p>}
      {row.reply && <p>答复：{row.reply}</p>}
      {row.update_available && <form className="stack" onSubmit={event => {
        event.preventDefault();
        const query = manualOrigin ? text(formValues(event), "query") : undefined;
        void op.perform(async () => {
          const result = await api.mutate<{ task_id: string }>(
            `/document-feedback/${id(row.id)}/requery`, { query }, undefined, "POST", !manualOrigin);
          await onQuery(result.task_id);
        });
      }}>
        <p>关联手册已有可用的新版本。</p>
        {manualOrigin && <label>要查询的问题<input name="query" required maxLength={2000} /></label>}
        <button disabled={op.busy}>按最新资料重新查询</button>
      </form>}
    </article>)}
    {ready && cursor && <button className="ghost" disabled={op.busy} onClick={() => void op.perform(() => refresh(cursor))}>加载更多反馈</button>}
    <Status message={op.message} bad={op.bad} />
  </section>;
}

export function FeedbackInbox() {
  const op = useOperation();
  const [rows, setRows] = useState<Assignment[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  async function refresh(next?: string) {
    if (!next) { setRows([]); setLoaded(false); }
    const result = await api.get<Page<Assignment>>("/knowledge/feedback" + (next ? "?cursor=" + id(next) : ""));
    setRows(previous => next ? [...previous, ...result.items] : result.items);
    setCursor(result.next_cursor);
    setLoaded(true);
  }
  useEffect(() => { void op.perform(() => refresh()); }, []);
  async function handle(row: Assignment, status: string, reply?: string) {
    await api.mutate(`/knowledge/feedback/${id(row.id)}`, { status, reply }, etag(row), "PATCH", !reply);
    await refresh();
  }
  return <section className="panel stack">
    <div className="section-heading"><h2>资料反馈受理</h2>
      <button className="ghost" disabled={op.busy} onClick={() => void op.perform(() => refresh())}>刷新待处理反馈</button>
    </div>
    {loaded && !rows.length && <p className="muted">当前范围内没有可受理的反馈。</p>}
    {rows.map(row => <article className="evidence" key={row.id}>
      <p className="muted">{labels[row.status]}</p>
      <p>{row.text}</p>
      <ul>{row.sources.map(source => <li key={source.file_url}><a href={source.file_url} target="_blank" rel="noopener noreferrer">
        {source.title} · {source.revision_label}{source.reference_only ? "（仅供参考）" : ""}
      </a></li>)}</ul>
      {row.reply && <p>答复：{row.reply}</p>}
      {row.assignment === "unclaimed" && <button disabled={op.busy} onClick={() => void op.perform(() => handle(row, "processing"))}>领取反馈</button>}
      {row.status === "processing" && row.assignment === "mine" && <form className="stack" onSubmit={event => {
        event.preventDefault();
        const reply = text(formValues(event), "reply");
        void op.perform(() => handle(row, "replied", reply), "答复已发送到员工的反馈入口。");
      }}>
        <label>给员工的答复<textarea name="reply" required maxLength={2000} rows={2} /></label>
        <button disabled={op.busy}>提交答复</button>
      </form>}
    </article>)}
    {cursor && <button className="ghost" disabled={op.busy} onClick={() => void op.perform(() => refresh(cursor))}>加载更多反馈</button>}
    <Status message={op.message} bad={op.bad} />
  </section>;
}
