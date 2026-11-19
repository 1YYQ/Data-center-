import { useState } from "react";
import { api, id } from "../../api";
import { formValues, Status, text, useOperation } from "../../components";

type Recipient = { user_id: string; expires_at: string | null };
type Share = Recipient & { status: string; answer_snapshot_id: string | null };
type Shares = { answer_snapshot_id: string | null; items: Share[] };

export function AnswerSharing({ taskId, snapshotId }: { taskId: string; snapshotId: string }) {
  const [selection, setSelection] = useState<{ items: Share[]; etag: string } | null>(null);
  const op = useOperation();
  const path = `/tasks/${id(taskId)}/shares`;
  const pending = api.pendingCommands().find(command => command.path === path && command.method === "PUT");
  const active = (selection?.items ?? []).filter(item => item.status === "active" && item.answer_snapshot_id === snapshotId);

  async function loadShares() {
    const { data, etag } = await api.request<Shares>(path);
    if (!etag || data.answer_snapshot_id !== snapshotId) {
      setSelection(null);
      throw Error("回答版本已变化，请刷新原查询后管理分享。");
    }
    setSelection({ items: data.items, etag });
  }

  async function replaceShares(entries: Recipient[]) {
    if (!selection) return;
    await api.mutate<Shares>(path, { answer_snapshot_id: snapshotId, entries }, selection.etag, "PUT");
    await loadShares();
  }

  return <section className="evidence" aria-label="单条回答分享">
    <fieldset className="operation-fields" disabled={op.busy} aria-busy={op.busy}>
      {pending ? <button className="ghost" type="button" onClick={() => void op.perform(async () => {
        await api.recoverCommand(pending);
        await loadShares();
      }, "分享操作回执已读取。")}>恢复原分享回执</button>
        : <button className="ghost" type="button" onClick={() => void op.perform(loadShares)}>
          {selection ? "刷新分享状态" : "分享这条回答"}
        </button>}
      {selection && !pending && <>
        <p className="muted">接收者须已有引用资料的读取权限，可在自己的查询记录中打开这条回答。</p>
        {active.length ? <ul>{active.map(({ user_id, expires_at }) => <li key={user_id}>
          <code>{user_id}</code>{expires_at && <span className="locator"> · {new Date(expires_at).toLocaleString("zh-CN")} 到期 </span>}
          <button className="ghost" type="button" aria-label={`撤回对 ${user_id} 的分享`} onClick={() => void op.perform(
            () => replaceShares(active.filter(item => item.user_id !== user_id).map(({ user_id, expires_at }) => ({ user_id, expires_at }))),
            "已撤回这条回答的分享。",
          )}>撤回分享</button>
        </li>)}</ul> : <p className="locator">当前没有有效分享。</p>}
        <form className="stack" onSubmit={event => {
          event.preventDefault();
          const values = formValues(event), form = event.currentTarget;
          void op.perform(async () => {
            const userId = text(values, "user_id");
            if (!userId) throw Error("请填写接收者用户编号。");
            if (active.some(item => item.user_id === userId)) throw Error("这条回答已分享给该接收者。");
            const expiryInput = text(values, "expires_at");
            const expiry = expiryInput ? new Date(expiryInput) : null;
            if (expiry && !(expiry.getTime() > Date.now())) throw Error("分享截止时间须晚于当前时间。");
            await replaceShares([...active.map(({ user_id, expires_at }) => ({ user_id, expires_at })),
              { user_id: userId, expires_at: expiry?.toISOString() ?? null }]);
            form.reset();
          }, "已保存分享。接收者需通过资料权限检查。");
        }}>
          <label>接收者用户编号<input name="user_id" required maxLength={200} /></label>
          <label>分享截止时间（可选）<input name="expires_at" type="datetime-local" /></label>
          <button>确认分享</button>
        </form>
      </>}
      <Status message={op.message} bad={op.bad} />
    </fieldset>
  </section>;
}
