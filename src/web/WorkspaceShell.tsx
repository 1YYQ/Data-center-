import { useEffect, useState, type ReactNode } from "react";
import { errorMessage } from "./api";
import { useSession } from "./session";

export function WorkspaceShell({ title, intro, role, children }: { title: string; intro: string; role?: string; children: ReactNode }) {
  const session = useSession(), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [online, setOnline] = useState(navigator.onLine);
  useEffect(() => { const update = () => setOnline(navigator.onLine); window.addEventListener("online", update); window.addEventListener("offline", update); return () => { window.removeEventListener("online", update); window.removeEventListener("offline", update); }; }, []);
  const path = location.pathname, km = path.includes("manage"), sa = /access-review|services|operations|accounts/.test(path);
  const roles = session.profile?.user.roles ?? [], capabilities = session.profile?.capabilities ?? [];
  const allowed = !role || roles.includes(role);
  const label = sa && roles.includes("system_admin") ? "系统管理员" : km && roles.includes("knowledge_manager") ? "知识管理员" : roles.includes("employee") ? "普通使用者" : roles.includes("knowledge_manager") ? "知识管理员" : roles.includes("system_admin") ? "系统管理员" : "账号";
  const navigation = sa
    ? [["/workspace/operations", "运行与异常", "◎"], ["/workspace/accounts", "账号与授权", "♙"], ["/workspace/access-review", "审批依据", "▤"], ...(capabilities.includes("system.configure") ? [["/workspace/services", "服务准入", "◇"]] : [])]
    : km ? [["/workspace/manage", "资料库与核验", "▤"], ["/workspace/manage?feedback=1", "反馈处理", "□"]]
      : [["/workspace/query", "工作台", "▦"], ["/workspace/manuals", "资料查阅", "▤"]];
  async function login(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return; const form = new FormData(event.currentTarget); setBusy(true); setError("");
    try { await session.login(String(form.get("username")), String(form.get("password"))); }
    catch (err) { setError(errorMessage(err)); } finally { setBusy(false); }
  }
  return <div className="workspace-frame">
    <a className="skip-link" href="#workspace-content">跳到工作内容</a>
    <header className="app-header"><a className="brand" href="/workspace/query"><span className="brand-mark" aria-hidden="true"><i/><i/><i/><i/></span>数据中心</a><span className="workspace-name">企业工作空间</span>
      <div className="app-account">{session.profile && <><span>{session.profile.user.display_name}</span><span className="role-label">{label}</span><details className="role-menu"><summary>切换工作区</summary><nav aria-label="已获准工作区">{roles.includes("employee") && <a href="/workspace/query">普通使用者</a>}{roles.includes("knowledge_manager") && <a href="/workspace/manage">知识管理员</a>}{roles.includes("system_admin") && <a href="/workspace/operations">系统管理员</a>}</nav></details><button className="ghost" disabled={busy} onClick={() => { setBusy(true); void session.logout().finally(() => setBusy(false)); }}>退出</button></>}</div>
    </header>
    <aside className="app-sidebar">{allowed && <><a className="primary-link" href={km ? "/workspace/manage#collect" : sa ? "/workspace/services" : "/workspace/query?new=1"}>{km ? "+ 收录资料" : sa ? "核对服务准入" : "+ 新建工作"}</a>
      <nav aria-label="工作区导航">{navigation.map(([href, name, icon]) => <a key={href} href={href} aria-current={path + location.search === href || (!location.search && path === href) ? "page" : undefined}><span aria-hidden="true">{icon}</span>{name}</a>)}</nav></>}
      <div className="sidebar-foot"><strong>安全使用提示</strong><p>仅在获准范围内处理资料。版本、来源与权限在使用时重新核对。</p></div>
    </aside>
    <main id="workspace-content" className={`app-main ${km ? "knowledge-main" : sa ? "admin-main" : "employee-main"}`} tabIndex={-1}>
      {session.loading ? <p role="status">正在恢复登录状态…</p> : session.suspended ? <section className="panel"><h1>暂时无法核对会话</h1><p role="alert">{session.error}</p><p>受保护内容已隐藏。连接恢复后只读核对原记录，不会重复提交工作。</p><button disabled={busy || !online} onClick={() => { setBusy(true); void session.recheck().finally(() => setBusy(false)); }}>重新核对会话</button></section> : !session.profile ? <section className="panel login-panel"><div><p className="eyebrow">企业工作空间</p><h1>登录数据中心</h1><p className="muted">继续自己的工作，查阅获准资料。</p></div><form className="stack" onSubmit={event => void login(event)}><label>账号<input name="username" autoComplete="username" required /></label><label>口令<input name="password" type="password" autoComplete="current-password" required /></label><button disabled={busy}>登录</button><p role="alert" className="error">{error || session.error}</p></form></section>
      : !allowed ? <section className="panel"><h1>此工作区不可访问</h1><p role="alert">当前账号没有进入本工作区所需的角色，请切换到已获准的工作区。</p></section>
      : <><div className="page-heading"><div><p className="eyebrow">{label} / 工作空间</p><h1>{title}</h1><p className="muted">{intro}</p></div></div>{online ? children : <section className="warning" role="alert">网络不可用，已隐藏受限内容。连接恢复后核对原工作，不会重新提交。</section>}</>}
    </main>
  </div>;
}
