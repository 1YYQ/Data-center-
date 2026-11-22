import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, errorMessage, StaleSessionError } from "./api";
import type { Profile, User } from "./api";

type Session = { profile: Profile | null; error: string; loading: boolean; suspended: boolean; recheck: () => Promise<void>; login: (username: string, password: string) => Promise<void>; logout: () => Promise<void> };
const Context = createContext<Session | null>(null);
const storageKey = "dc-workspace-session";
export function SessionProvider({ children }: { children: ReactNode }) {
  const [profile, setProfile] = useState<Profile | null>(null), [error, setError] = useState(""), [loading, setLoading] = useState(true);
  const [suspended, setSuspended] = useState(false), checking = useRef(false), mounted = useRef(true);
  function clear(message: string) { api.setSession(null, null); sessionStorage.removeItem(storageKey); sessionStorage.removeItem("dc-workspace-query"); setProfile(null); setSuspended(false); setError(message); }
  // A failed read is not proof of logout. Unmount protected views while retaining
  // only the existing session binding and opaque work/receipt identities.
  async function recheck() {
    if (checking.current || !api.owner || !navigator.onLine) return;
    checking.current = true;
    try {
      const current = await api.get<Profile>("/me");
      if (!mounted.current) return;
      if (current.user.id !== api.owner) { clear("页面登录身份已变化，请重新登录。"); return; }
      setProfile(current); setSuspended(false); setError("");
    } catch (error) {
      if (mounted.current && !(error instanceof StaleSessionError) && api.owner) { setSuspended(true); setError(errorMessage(error)); }
    } finally { checking.current = false; }
  }
  api.onAccessLost = clear;
  useEffect(() => {
    let active = true; mounted.current = true;
    async function restore() {
      try {
        const stored = JSON.parse(sessionStorage.getItem(storageKey) || "null");
        if (!stored?.owner || !stored.csrf) return;
        api.setSession(stored.owner, stored.csrf);
        await recheck();
      } catch (error) { if (active) clear(errorMessage(error)); }
      finally { if (active) setLoading(false); }
    }
    void restore(); return () => { active = false; mounted.current = false; };
  }, []);
  useEffect(() => {
    if (!profile && !suspended) return;
    const verify = () => { if (!document.hidden) void recheck(); };
    const timer = setInterval(() => void verify(), 15000);
    window.addEventListener("focus", verify); document.addEventListener("visibilitychange", verify);
    window.addEventListener("online", verify);
    return () => { clearInterval(timer); window.removeEventListener("focus", verify); window.removeEventListener("online", verify); document.removeEventListener("visibilitychange", verify); };
  }, [profile?.user.id, suspended]);
  async function login(username: string, password: string) {
    setError("");
    try {
      const response = await api.request<{ user: User; csrf_token: string }>("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      api.setSession(response.data.user.id, response.data.csrf_token);
      const current = await api.get<Profile>("/me");
      sessionStorage.setItem(storageKey, JSON.stringify({ owner: current.user.id, csrf: response.data.csrf_token }));
      setProfile(current); setSuspended(false);
    } catch (error) { clear(errorMessage(error)); throw error; }
  }
  async function logout() { try { await api.request("/auth/logout", { method: "POST" }); } finally { clear(""); } }
  return <Context.Provider value={{ profile, error, loading, suspended, recheck, login, logout }}>{children}</Context.Provider>;
}
export function useSession() { const session = useContext(Context); if (!session) throw Error("缺少登录工作区"); return session; }
