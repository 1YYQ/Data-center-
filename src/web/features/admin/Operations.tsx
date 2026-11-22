import { useEffect, useState } from "react";
import { api } from "../../api";
import { Workspace, Status, useOperation } from "../../components";
import { stateLabel } from "../../labels";
import { useSession } from "../../session";

type OperationTask = { id: string; status: string; error_code: string | null; created_at: string };
export function Operations() {
  return <Workspace title="运行与异常" eyebrow="" intro="核对服务状态与脱敏任务，按原任务边界恢复。" role="system_admin"><OperationsContent/></Workspace>;
}
function OperationsContent() {
  const op = useOperation(), { profile } = useSession(), [tasks, setTasks] = useState<OperationTask[]>([]);
  async function refresh() { const data = await api.get<{ items: OperationTask[] }>("/admin/operations/tasks"); setTasks(data.items); }
  useEffect(() => { void op.perform(refresh); }, []);
  return <div className="stack"><section className="panel"><div className="section-heading"><h2>恢复检查</h2><button className="ghost" disabled={op.busy} onClick={() => void op.perform(refresh)}>刷新运行状态</button></div><p className="warning">连接可达不代表准入有效。保留原任务、调用回执和预算，不代员工重新执行业务查询。</p><ol className="recovery-steps"><li><strong>核对服务配置</strong><p>查看固定修订、自检回执与有效状态；执行合成自检前明确外发和费用范围。</p></li><li><strong>核对逐用途准入</strong><p>资料范围、模型身份、留存、有效期与预算须全部有效。</p>{profile!.capabilities.includes("system.configure") && <a href="/workspace/services">核对服务准入 →</a>}</li><li><strong>接续原任务</strong><p>消费者恢复后接续原队列；需要手动重试时，由任务所有者在原工作中操作。调用结果未知只核对状态，不重新提交。</p></li></ol><p className="locator">本地备份按已确认试点决定保持停用。本页面不会安装、启动或执行备份。</p></section><section className="panel"><h2>任务状态（脱敏）</h2><p className="muted">系统管理员默认不读取问题、答案或业务资料正文。</p><table><thead><tr><th>任务标识</th><th>当前状态</th><th>异常与处理边界</th></tr></thead><tbody>{tasks.map(row => <tr key={row.id}><td><code>{row.id}</code></td><td>{stateLabel(row.status)}</td><td>{row.error_code ? "执行遇到异常，请核对服务与原回执。" : "以当前执行状态为准"}{row.status === "failed" && "；由原任务所有者核对是否可重试。"}</td></tr>)}</tbody></table>{!tasks.length && <p className="empty">当前没有可列出的任务记录。</p>}</section><Status message={op.message} bad={op.bad}/></div>;
}
