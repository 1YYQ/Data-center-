import { useEffect, useState } from "react";
import { api, errorMessage, id } from "../../api";
import { Workspace } from "../../components";
import { createProjectionLease } from "../../projectionLease";
import { SourceReader } from "../../SourceReader";
import { stateLabel } from "../../labels";
import { FeedbackPanel } from "../feedback";

type Version = { id: string; revision_label: string | null; model_scope: string[]; publish_status: string; applicability_status: string; content_review_status: string };
type Manual = { id: string; title: string; versions: Version[] };
export function Manuals({ legacyVersionId }: { legacyVersionId?: string }) {
  return <Workspace title="资料查阅" eyebrow="" intro="按型号或文件名称查找当前有权查看的资料。"><ManualList legacyVersionId={legacyVersionId}/></Workspace>;
}
function ManualList({ legacyVersionId }: { legacyVersionId?: string }) {
  const params = new URLSearchParams(location.search), version = params.get("version") || legacyVersionId;
  const [items, setItems] = useState<Manual[]>([]), [term, setTerm] = useState(""), [message, setMessage] = useState("正在读取授权资料…");
  useEffect(() => { let active = true, sequence = 0;
    const lease = createProjectionLease(() => { setItems([]); setMessage("资料访问资格待重新核对，已隐藏目录。"); });
    async function load() {
      if (!navigator.onLine || document.hidden) return;
      const started = performance.now(), token = ++sequence;
      try {
        const libraries = await api.get<{ items: { id: string }[] }>("/knowledge-bases"), rows: Manual[] = [];
        for (const library of libraries.items) rows.push(...(await api.get<{ items: Manual[] }>(`/knowledge-bases/${id(library.id)}/documents`)).items);
        if (active && token === sequence && navigator.onLine && lease.renew(started)) { setItems(rows); setMessage(rows.length ? "" : "当前授权范围内暂无可查阅资料。"); }
      } catch (error) { if (active && token === sequence && navigator.onLine) { setItems([]); setMessage(errorMessage(error)); } }
    }
    const offline = () => { sequence++; setItems([]); setMessage("网络不可用，已隐藏资料目录。"); };
    void load(); const timer = setInterval(() => { if (!document.hidden) void load(); }, 10000);
    window.addEventListener("offline", offline); window.addEventListener("focus", load); document.addEventListener("visibilitychange", load); window.addEventListener("pageshow", load);
    return () => { active = false; sequence++; lease.dispose(); clearInterval(timer); window.removeEventListener("offline", offline); window.removeEventListener("focus", load); document.removeEventListener("visibilitychange", load); window.removeEventListener("pageshow", load); };
  }, []);
  const doc = params.get("document") || items.find(item => item.versions.some(row => row.id === version))?.id;
  if (legacyVersionId && !doc) return <p role="status">{message || "此版本当前不可访问，请返回资料查阅。"}</p>;
  if (doc && version) return <div className="manual-detail"><a href="/workspace/manuals">← 返回资料查阅</a><SourceReader documentId={doc} versionId={version} page={Math.max(1, Number(params.get("page")) || 1)} evidencePath={params.get("task") && params.get("evidence") ? `/tasks/${id(params.get("task")!)}/evidence/${id(params.get("evidence")!)}` : undefined}/><FeedbackPanel origin={{ document_version_id: version }} onQuery={task => location.assign("/workspace/query?task=" + id(task))}/></div>;
  const found = items.filter(item => (item.title + " " + item.versions.flatMap(v => v.model_scope).join(" ")).toLowerCase().includes(term.toLowerCase()));
  return <section className="panel manual-list"><label>筛选已载入资料<input type="search" value={term} onChange={event => setTerm(event.target.value)} placeholder="设备型号或文件名称" /></label><p role="status">{message}</p>{found.map(item => <article key={item.id} className="manual-row"><h2>{item.title}</h2>{item.versions.map(v => <div key={v.id} className="manual-version"><span>{v.revision_label || "版本标识待确认"} · {v.model_scope.join("、") || "适用型号待确认"}</span><span>{stateLabel(v.publish_status)}{v.publish_status === "draft" && " · 仅供参考"}</span><a href={`/workspace/manuals?document=${id(item.id)}&version=${id(v.id)}`}>查阅原文 →</a></div>)}</article>)}{!message && !found.length && <p>未找到匹配资料，可调整型号或文件名称。</p>}</section>;
}
