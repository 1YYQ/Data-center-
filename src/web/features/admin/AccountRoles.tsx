import { useState } from "react";
import { api } from "../../api";

const roleNames = {employee:"普通使用者", knowledge_manager:"知识管理员", system_admin:"系统管理员"};
export function AccountRoles({account, perform, reload}: {account:{id:string; roles:string[]; authz_revision:number}; perform:(action:()=>Promise<void>,message?:string)=>Promise<void>; reload:()=>Promise<void>}) {
  const [roles, setRoles] = useState(account.roles);
  const changed = [...roles].sort().join() !== [...account.roles].sort().join();
  return <form className="stack space" onSubmit={event => {event.preventDefault(); void perform(async () => {
    if (!roles.length || !changed) throw Error("请至少保留一个角色，并核对本次变更。");
    if (!window.confirm("确认更新角色？该用户现有会话将失效；资料权限仍由独立审批与授权决定。")) return;
    await api.mutate(`/admin/users/${encodeURIComponent(account.id)}/roles`, {role_codes: roles}, `"${account.authz_revision}"`, "PUT");
    await reload();
  }, "角色已更新，原会话需重新登录；资料授权未自动扩大。");}}>
    <strong>调整平台角色</strong>{Object.entries(roleNames).map(([key,label]) => <label key={key} className="toggle"><input type="checkbox" checked={roles.includes(key)} onChange={event => setRoles(current => event.target.checked ? [...current,key] : current.filter(role => role !== key))}/>{label}</label>)}
    <button className="ghost" disabled={!roles.length || !changed}>确认更新角色</button>
  </form>;
}
