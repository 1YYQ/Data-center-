import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, errorMessage, id } from "./api";
import { useSession } from "./session";
import { createProjectionLease } from "./projectionLease";

export function SourceLink({ documentId, versionId, page = 1, evidencePath, children }: { documentId: string; versionId: string; page?: number; evidencePath?: string; children: ReactNode }) {
  const [open, setOpen] = useState(false), dialog = useRef<HTMLDialogElement>(null);
  const binding = evidencePath?.match(/^\/tasks\/([^/]+)\/evidence\/([^/]+)$/);
  const href = `/workspace/manuals?document=${id(documentId)}&version=${id(versionId)}&page=${page}` + (binding ? `&task=${binding[1]}&evidence=${binding[2]}` : "");
  useEffect(() => { if (open) dialog.current?.showModal(); }, [open]);
  return <><a href={href} onClick={event => { if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return; event.preventDefault(); setOpen(true); }}>{children}</a>{open && <dialog className="source-dialog" ref={dialog} onClose={() => setOpen(false)}><div className="section-heading"><h2>核对引用原文</h2><button type="button" className="ghost" onClick={() => dialog.current?.close()}>返回原工作</button></div><SourceReader documentId={documentId} versionId={versionId} page={page} evidencePath={evidencePath}/></dialog>}</>;
}

export function SourceReader({ documentId, versionId, page = 1, evidencePath }: { documentId: string; versionId: string; page?: number; evidencePath?: string }) {
  const { profile } = useSession(), [url, setUrl] = useState(""), [message, setMessage] = useState("正在核对原文读取权限…"), [mode, setMode] = useState("");
  const [retry, setRetry] = useState(0), frame = useRef<HTMLIFrameElement>(null);
  useEffect(() => {
    let active = true, objectUrl = "", sequence = 0, checking: number | null = null;
    setUrl(""); setMessage("正在核对原文读取权限…");
    const base = `/documents/${id(documentId)}/versions/${id(versionId)}/file`;
    function clear() { if (active) setUrl(""); if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = ""; }
    const lease = createProjectionLease(() => { clear(); if (active) setMessage("原文访问资格待重新核对，已隐藏内容。"); });
    async function check(load = false) {
      if (!active || checking !== null || !navigator.onLine) return;
      const token = ++sequence; checking = token;
      const started = performance.now();
      const current = () => active && token === sequence && navigator.onLine;
      try {
        if (evidencePath) await api.get(evidencePath);
        if (!current()) return;
        const access = await api.get<{ read_mode: string }>(base + "-access");
        if (!current()) return;
        if (!lease.renew(started)) return;
        setMode(access.read_mode);
        if (load || !objectUrl) {
          const blob = await api.blob(base);
          if (!current()) return;
          if (!lease.renew(started)) return;
          objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); setMessage("");
        }
      } catch (error) { if (current()) { clear(); setMessage(errorMessage(error)); } }
      finally { if (checking === token) checking = null; }
    }
    function hide(message: string) { sequence++; checking = null; clear(); setMessage(message); }
    const offline = () => hide("网络不可用，已隐藏原文。恢复连接后请重新核对。");
    const focus = () => { if (!document.hidden && navigator.onLine) { hide("正在重新核对原文权限…"); void check(true); } };
    const visible = () => { if (document.hidden) hide("原文已隐藏，返回页面后重新核对。"); else focus(); };
    void check(true);
    const interval = setInterval(() => { if (!document.hidden) void check(!objectUrl); }, 10000);
    window.addEventListener("focus", focus); window.addEventListener("offline", offline);
    document.addEventListener("visibilitychange", visible); window.addEventListener("pageshow", focus);
    return () => { active = false; sequence++; lease.dispose(); clearInterval(interval); window.removeEventListener("focus", focus); window.removeEventListener("offline", offline); document.removeEventListener("visibilitychange", visible); window.removeEventListener("pageshow", focus); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [documentId, versionId, evidencePath, profile?.user.id, retry]);
  return <section className="source-reader" aria-label="受控原文阅读器">
    <div className="reader-toolbar"><strong>原始 PDF</strong><span>PDF 第 {page} 页</span><button type="button" className="ghost" onClick={() => setRetry(value => value + 1)}>重新核对原文</button></div>
    {mode === "reference_only" && <p className="warning">待确认或待核验，仅供原文参考，不能作为正式答案依据。</p>}
    {mode === "historical" && <p className="warning">当前为引用的历史版本，请核对其适用条件。</p>}
    {url ? <iframe ref={frame} title={`原始PDF 第${page}页`} src={url + `#page=${page}&toolbar=1`} /> : <div className="reader-unavailable"><p role="status">{message}</p></div>}
    <p className="locator">按当前权限读取；页序号与印刷页码分别记录。</p>
  </section>;
}
