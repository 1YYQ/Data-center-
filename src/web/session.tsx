import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, errorMessage } from "./api";
import type { Profile, User } from "./api";

type Session = { profile: Profile | null; error: string; loading: boolean; login: (username: string, password: string) => Promise<void>; logout: () => Promise<void> };
const Context = createContext<Session | null>(null);
const storageKey = "dc-workspace-session";
export function SessionProvider({ children }: { children: ReactNode }) {
  const [profile, setProfile] = useState<Profile | null>(null), [error, setError] = useState(""), [loading, setLoading] = useState(true);
  function clear(message: string) { api.setSession(null, null); sessionStorage.removeItem(storageKey); sessionStorage.removeItem("dc-workspace-query"); setProfile(null); setError(message); }
  api.onAccessLost = clear;
  useEffect(() => {
    let active = true;
    async function restore() {
      try {
        const stored = JSON.parse(sessionStorage.getItem(storageKey) || "null");
        if (!stored?.owner || !stored.csrf) return;
        api.setSession(stored.owner, stored.csrf);
        const current = await api.get<Profile>("/me");
        if (current.user.id !== stored.owner) { clear("账号已改变，请重新登录。"); return; }
        if (active) setProfile(current);
      } catch (error) { if (active) clear(errorMessage(error)); }
      finally { if (active) setLoading(false); }
    }
    void restore(); return () => { active = false; };
  }, []);
  async function login(username: string, password: string) {
    setError("");
    try {
      const response = await api.request<{ user: User; csrf_token: string }>("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      api.setSession(response.data.user.id, response.data.csrf_token);
      const current = await api.get<Profile>("/me");
      sessionStorage.setItem(storageKey, JSON.stringify({ owner: current.user.id, csrf: response.data.csrf_token }));
      setProfile(current);
    } catch (error) { clear(errorMessage(error)); throw error; }
  }
  async function logout() { try { await api.request("/auth/logout", { method: "POST" }); } finally { clear(""); } }
  return <Context.Provider value={{ profile, error, loading, login, logout }}>{children}</Context.Provider>;
}
export function useSession() { const session = useContext(Context); if (!session) throw Error("缺少登录工作区"); return session; }
