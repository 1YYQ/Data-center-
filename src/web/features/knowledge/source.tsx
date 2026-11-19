import { Field, formValues, text } from "../../components";
import { api } from "../../api";
export type SourceMetadata = { channel?: string | null; source_description?: string | null; received_at?: string | null; source_party?: string | null; traceable_ref?: string | null };
export type SourceView = { source_metadata: SourceMetadata; revision: number; etag: string };
export function SourceForm({ source, base, draft, perform, onChange }: { source: SourceView; base: string; draft: boolean; perform: (action: () => Promise<void>, success?: string) => Promise<void>; onChange: () => Promise<void> }) {
  const metadata = source.source_metadata;
  const received = metadata.received_at ? new Date(metadata.received_at) : null;
  const localTime = received && Number.isFinite(received.valueOf()) ? new Date(received.valueOf() - received.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";
  return <section className="source-form"><h3>资料来源登记</h3><p className="muted">登记实际来源，未知字段留空。来源变化会重新核对已有确认依赖；来源登记本身不证明适用性或内容正确。</p><form className="stack" onSubmit={event => { event.preventDefault(); const values = formValues(event); void perform(async () => {
    const receivedAt = text(values, "received_at");
    await api.mutate(base + "/source-metadata", { channel: text(values, "channel") || null, source_description: text(values, "source_description") || null, received_at: receivedAt ? new Date(receivedAt).toISOString() : null, source_party: text(values, "source_party") || null, traceable_ref: text(values, "traceable_ref") || null }, source.etag, "PATCH");
    await onChange();
  }, "资料来源已保存；请重新核对业务依据和发布条件。"); }}><fieldset className="operation-fields stack" disabled={!draft}><Field label="来源渠道" name="channel" value={metadata.channel || ""} required={false} /><Field label="来源说明" name="source_description" value={metadata.source_description || ""} required={false} /><Field label="接收时间（未知可留空）" name="received_at" type="datetime-local" value={localTime} required={false} /><Field label="来源单位（可选）" name="source_party" value={metadata.source_party || ""} required={false} /><Field label="可追溯编号（可选）" name="traceable_ref" value={metadata.traceable_ref || ""} required={false} /><button>保存资料来源</button></fieldset>{!draft && <p className="muted">此版本已发布；如需变更来源，请先上传新修订。</p>}</form></section>;
}
