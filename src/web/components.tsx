import { useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, errorMessage, StaleSessionError } from "./api";
import { useSession } from "./session";

export function useOperation() {
  const lock = useRef(false), [busy, setBusy] = useState(false), [message, setMessage] = useState(""), [bad, setBad] = useState(false);
  async function perform(action: () => Promise<void>, success?: string) {
    if (lock.current) return; lock.current = true; setBusy(true); setMessage("");
    try { await action(); if (success) { setMessage(success); setBad(false); } }
    catch (error) { if (!(error instanceof StaleSessionError)) { setMessage(errorMessage(error)); setBad(true); } }
    finally { lock.current = false; setBusy(false); }
  }
  return { busy, message, bad, perform, say: (value: string) => { setMessage(value); setBad(false); } };
}
export function Status({ message, bad = false }: { message: string; bad?: boolean }) { return <p className={`status ${bad ? "bad" : "success"}`} role={bad ? "alert" : "status"}>{message}</p>; }
export function Field({ label, name, type = "text", required = true, value }: { label: string; name: string; type?: string; required?: boolean; value?: string }) { return <label>{label}<input name={name} type={type} required={required} defaultValue={value} autoComplete={name === "username" ? "username" : undefined} /></label>; }
export const formValues = (event: FormEvent<HTMLFormElement>) => new FormData(event.currentTarget);
export const text = (form: FormData, name: string) => String(form.get(name) || "").trim();
export const selectedValues = (select: HTMLSelectElement) => Array.from(select.selectedOptions, option => option.value);
export function PendingOperations({ onRecovered, perform }: { onRecovered: () => Promise<void>; perform: (action: () => Promise<void>, success?: string) => Promise<void> }) {
  const commands = api.pendingCommands().filter(command => !command.path.startsWith("/knowledge/queries") && !command.path.startsWith("/tasks/"));
  if (!commands.length) return null;
  const names: Record<string, string> = { "source-metadata": "登记资料来源", publish: "发布", withdraw: "撤回", "publication-preview": "发布条件预检", "review-sets": "创建核验集", review: "保存内容核验", verification: "记录核查", "evidence-refs": "关联材料", "business-confirmations": "登记真实确认", "business-approvals": "登记外部审批", "reference-access": "设置参考开放", "index-generations": "构建索引", revocations: "登记外部撤销" };
  return <aside className="warning" aria-label="待核对的原操作"><p>上次操作结果未知。可读取原操作回执；恢复不会创建另一项操作，服务端仍会核对当前权限。</p>{commands.map(command => <div key={command.key}><p className="locator">{names[command.path.split("/").at(-1)!] || "原操作"} · {command.path.split("/").filter((_, index, parts) => parts[index - 1] === "documents" || parts[index - 1] === "versions" || parts[index - 1] === "business-approvals").join(" / ")}</p><button className="ghost" onClick={() => void perform(async () => { await api.recoverCommand(command); await onRecovered(); }, "原操作回执已读取，请核对当前状态。")}>恢复原操作回执</button></div>)}</aside>;
}
export function Workspace({ title, eyebrow, intro, role, children }: { title: string; eyebrow: string; intro: string; role?: string; children: ReactNode }) {
  const session = useSession(), op = useOperation();
  const allowed = session.profile && (!role || session.profile.user.roles.includes(role));
  return <main className={`shell ${role === "system_admin" ? "approvals-shell" : ""}`}><header className="topbar"><div><p className="eyebrow">DATA CENTER / {eyebrow}</p><h1>{title}</h1><p className="muted">{intro}</p></div><div className="account"><a className="ghost" href="/workspace/query">资料查询</a>{session.profile?.user.roles.includes("knowledge_manager") && <a className="ghost" href="/workspace/manage">资料治理</a>}{session.profile?.user.roles.includes("system_admin") && <a className="ghost" href="/workspace/access-review">授权审批</a>}{session.profile?.capabilities.includes("system.configure") && <a className="ghost" href="/workspace/services" aria-current={location.pathname.replace(/\/$/, "") === "/workspace/services" ? "page" : undefined}>服务配置</a>}<span className="session-chip">{session.profile?.user.display_name || "未登录"}</span>{session.profile && <button className="ghost" disabled={op.busy} onClick={() => void op.perform(session.logout)}>退出</button>}</div></header>{session.loading ? <p role="status">正在恢复登录状态…</p> : !session.profile ? <section className="panel login-panel"><div><p className="eyebrow">开始使用</p><h2>登录工作区</h2><p className="muted">使用现有账号。只有当前职责范围内的资料与记录可供查看、维护。</p></div><form className="stack" onSubmit={event => { event.preventDefault(); const values = formValues(event); const form = event.currentTarget; void op.perform(async () => { await session.login(text(values, "username"), String(values.get("password") || "")); form.reset(); }); }}><Field label="账号" name="username" /><label>口令<input name="password" type="password" autoComplete="current-password" required /></label><button disabled={op.busy}>登录</button><p className="error" role="alert">{session.error || op.message}</p></form></section> : !allowed ? <section className="panel"><p role="alert">当前账号没有进入本工作区所需的角色，请联系管理员。</p></section> : children}</main>;
}


