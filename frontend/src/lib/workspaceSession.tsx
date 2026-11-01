import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { createLocalWorkspace, friendlyError } from "../api";
import { devLogin, getToken, onTokenChange, setToken } from "../auth";
import { Button, Spinner } from "../components/ui";
import type { LocalWorkspaceSession } from "../types";
import { useMeta } from "./meta";

const KEY = "zhixu.localWorkspace";
type Stored = LocalWorkspaceSession & { started_at: number };
const Context = createContext<{ session: Stored | null; restart: () => Promise<void> }>({ session: null, restart: async () => {} });
export const useWorkspaceSession = () => useContext(Context);
let initializing: Promise<Stored> | null = null;

function stored(): Stored | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(KEY) || "null") as Stored | null;
    return value && Date.now() < value.started_at + (value.expires_in - 30) * 1000 ? value : null;
  } catch { return null; }
}

async function initialize(fresh = false): Promise<Stored> {
  if (!fresh) {
    const previous = stored();
    if (previous) { setToken("customer", previous.token); return previous; }
  }
  if (!initializing) {
    initializing = createLocalWorkspace().then(session => {
      const value = { ...session, started_at: Date.now() };
      sessionStorage.setItem(KEY, JSON.stringify(value));
      setToken("customer", value.token);
      return value;
    }).finally(() => { initializing = null; });
  }
  return initializing;
}

export function WorkspaceSessionProvider({ children }: { children: ReactNode }) {
  const { meta } = useMeta();
  const [session, setSession] = useState<Stored | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(Boolean(meta?.local_workspace));
  const restart = async () => {
    setLoading(true); setError(null);
    try { setSession(await initialize(true)); }
    catch (err) { setError(friendlyError(err)); }
    finally { setLoading(false); }
  };
  useEffect(() => {
    if (!meta?.local_workspace) return;
    let active = true;
    initialize().then(value => { if (active) setSession(value); })
      .catch(err => { if (active) setError(friendlyError(err)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [meta?.local_workspace]);
  useEffect(() => onTokenChange(role => {
    if (meta?.local_workspace && role === "customer" && !getToken("customer")) {
      setError("当前体验会话已失效，请重新连接。已有处理记录仍保留。");
    }
  }), [meta?.local_workspace]);
  if (loading || error) return <div className="flex h-full flex-col items-center justify-center gap-4 text-sm text-muted">
    {error ? <><p>{error}</p><Button onClick={() => void restart()}>重新连接</Button></> : <><Spinner />正在准备你的退款工作台…</>}
  </div>;
  return <Context.Provider value={{ session, restart }}>{children}</Context.Provider>;
}

/** Customer identity remains fixed while the existing local-only settings API authenticates. */
export function LocalSettingsAccess({ children }: { children: ReactNode }) {
  const { meta } = useMeta();
  const [ready, setReady] = useState(!meta?.local_workspace);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!meta?.local_workspace) return;
    let active = true;
    setError(false);
    devLogin("admin").then(() => { if (active) setReady(true); })
      .catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [meta?.local_workspace, attempt]);
  useEffect(() => onTokenChange(role => {
    if (meta?.local_workspace && role === "admin" && !getToken("admin")) {
      setReady(false); setAttempt(value => value + 1);
    }
  }), [meta?.local_workspace]);
  if (!ready) return <div className="flex h-full items-center justify-center gap-3 text-sm text-muted">
    {error ? <><span>无法打开本地模型设置。</span><Button onClick={() => setAttempt(n => n + 1)}>重试</Button></> : <><Spinner />正在读取模型设置…</>}
  </div>;
  return children;
}
