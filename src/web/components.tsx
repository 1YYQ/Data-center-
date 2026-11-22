import { useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { api, errorMessage, StaleSessionError } from "./api";
import { useSession } from "./session";
import { WorkspaceShell } from "./WorkspaceShell";

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
  void eyebrow;
  return <WorkspaceShell title={title} intro={intro} role={role}>{children}</WorkspaceShell>;
}


