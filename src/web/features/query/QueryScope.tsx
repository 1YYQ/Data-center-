import { useEffect, useState } from "react";
import { api } from "../../api";
import { createProjectionLease } from "../../projectionLease";

type Library = {id: string; name: string};
export function QueryScope({value, onChange, onValidity}: {value: string[] | null; onChange: (value: string[] | null) => void; onValidity: (valid: boolean) => void}) {
  const [libraries, setLibraries] = useState<Library[] | null>(null), [error, setError] = useState("");
  useEffect(() => {
    let active = true, running = false;
    const lease = createProjectionLease(() => { if (active) setLibraries(null); });
    async function refresh() {
      if (running || document.hidden) return;
      running = true; const started = performance.now();
      try {
        const items: Library[] = []; let offset: number | null = 0;
        while (offset !== null) {
          const page: {items: Library[]; next_offset: number | null} = await api.get(`/knowledge/query-scopes?limit=100&offset=${offset}`);
          if (!active) return;
          items.push(...page.items); offset = page.next_offset;
        }
        if (active && lease.renew(started)) {setLibraries(items); setError("");}
      } catch {if (active) {lease.invalidate(); setError("暂时无法核对可选资料库。");}}
      finally {running = false;}
    }
    void refresh(); const timer = setInterval(() => void refresh(),10000);
    return () => {active = false; lease.dispose(); clearInterval(timer);};
  }, []);
  const valid = value === null || Boolean(libraries && value.length && value.every(ref => libraries.some(row => row.id === ref)));
  useEffect(() => onValidity(valid), [valid, onValidity]);
  return <details className="query-scope"><summary>查询范围：{value === null ? "当前全部获权资料" : `已选择 ${value.length} 个资料库`}</summary>
    <label className="toggle"><input type="checkbox" checked={value === null} onChange={event => onChange(event.target.checked ? null : [])}/>使用当前全部获权资料</label>
    <p className="locator">缩小范围只影响接下来的查询；旧答复保留原范围。实际使用仍核对版本和权限。</p>
    {libraries === null ? <p role="status">{error || "正在核对可选资料库…"}</p> : <div className="scope-options">{libraries.map(row => <label className="toggle" key={row.id}><input type="checkbox" checked={value === null || value.includes(row.id)} onChange={event => {
      const current = value === null ? libraries.map(row => row.id) : value;
      onChange(event.target.checked ? [...new Set([...current,row.id])] : current.filter(ref => ref !== row.id));
    }}/>{row.name}</label>)}{!libraries.length && <p>当前没有可选的资料查询范围。</p>}</div>}
    {!valid && <p className="warning">请至少选择一个当前可访问的资料库；原先范围失效时，需重新选择。</p>}
  </details>;
}
