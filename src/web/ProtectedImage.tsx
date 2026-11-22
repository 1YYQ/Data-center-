import { useEffect, useState } from "react";
import { api, errorMessage } from "./api";
import { useSession } from "./session";
import { createProjectionLease } from "./projectionLease";

/** Binary previews use the same page-session binding as JSON, never raw cookies. */
export function ProtectedImage({ src, accessPath, alt }: { src: string; accessPath: string; alt: string }) {
  const { profile } = useSession(), [url, setUrl] = useState(""), [message, setMessage] = useState("正在核对并读取原图…");
  useEffect(() => {
    let active = true, objectUrl = "", sequence = 0, running = false;
    function clear() { if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = ""; if (active) setUrl(""); }
    const lease = createProjectionLease(() => { clear(); if (active) setMessage("原图访问资格待核对，已隐藏内容。"); });
    async function load() {
      if (!active || running || !navigator.onLine || document.hidden) return;
      const token = ++sequence, started = performance.now(); running = true;
      const current = () => active && token === sequence && navigator.onLine;
      try {
        await api.get(accessPath);
        if (!current() || !lease.renew(started)) return;
        if (!objectUrl) { const data = await api.blob(src); if (!current() || !lease.renew(started)) return; objectUrl = URL.createObjectURL(data); setUrl(objectUrl); setMessage(""); }
      } catch (error) { if (current()) { clear(); setMessage(errorMessage(error)); } }
      finally { if (token === sequence) running = false; }
    }
    function hide() { sequence++; running = false; clear(); setMessage("原图已隐藏，重新核对后显示。"); }
    function visible() { hide(); if (!document.hidden) void load(); }
    void load(); const timer = setInterval(() => void load(), 10000);
    window.addEventListener("offline", hide); window.addEventListener("focus", load); document.addEventListener("visibilitychange", visible);
    return () => { active = false; sequence++; clearInterval(timer); lease.dispose(); clear(); window.removeEventListener("offline", hide); window.removeEventListener("focus", load); document.removeEventListener("visibilitychange", visible); };
  }, [src, accessPath, profile?.user.id]);
  return url ? <img src={url} alt={alt} loading="lazy" onError={() => { URL.revokeObjectURL(url); setUrl(""); setMessage("原图无法显示，请核对原PDF。"); }}/> : <span className="locator" role="status">{message}</span>;
}
