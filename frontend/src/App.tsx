import { ServerCrash } from "lucide-react";
import { Navigate, Route, Routes } from "react-router-dom";
import { WorkspaceTopbar } from "./components/WorkspaceNav";
import Logo from "./components/Logo";
import { Button, Spinner } from "./components/ui";
import { getDemo } from "./lib/demo";
import { useMeta } from "./lib/meta";
import { useWorkspaceMotion } from "./lib/useWorkspaceMotion";
import Chat from "./pages/Chat";
import NotFound from "./pages/NotFound";
import Settings from "./pages/Settings";
import Welcome from "./pages/Welcome";
import { LocalSettingsAccess, WorkspaceSessionProvider } from "./lib/workspaceSession";

function Splash() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 text-center">
      <Logo size={48} />
      <div className="flex items-center gap-2 text-sm text-muted">
        <Spinner className="text-brand" /> 正在连接客服工作空间…
      </div>
      <p className="max-w-xs text-xs text-subtle">
        正在读取服务状态与当前配置，请稍候。
      </p>
    </div>
  );
}

function Offline() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 px-6 text-center">
      <ServerCrash size={40} className="text-bad" />
      <div>
        <h1 className="text-lg font-semibold">后端服务没有响应</h1>
        <p className="mt-1 text-sm text-muted">可能正在重启，请稍后再试。</p>
      </div>
      <Button variant="primary" onClick={() => window.location.reload()}>
        重试
      </Button>
    </div>
  );
}

/** In demo mode, visitors need a sandbox before they can chat. */
function RequireSession({ children }: { children: JSX.Element }) {
  const { meta } = useMeta();
  if (meta?.auth_mode === "demo" && !getDemo())
    return <Navigate to="/" replace />;
  return children;
}

/** Evaluated per render (not once in App) so resetting the sandbox lands here. */
function Home() {
  const { meta } = useMeta();
  return meta?.auth_mode === "demo" && !getDemo() ? (
    <Welcome />
  ) : (
    <Navigate to="/chat" replace />
  );
}

export default function App() {
  const { meta, loading, error } = useMeta();
  if (loading) return <Splash />;
  if (error || !meta) return <Offline />;
  return <WorkspaceSessionProvider><WorkspaceContent /></WorkspaceSessionProvider>;
}

function WorkspaceContent() {
  const motionRoot = useWorkspaceMotion(true);
  return (
    <div ref={motionRoot} className="workspace-shell">
      <div className="workspace-body">
        <WorkspaceTopbar />
        <main className="workspace-main">
          <Routes>
            <Route path="/" element={<Home />} />
            <Route
              path="/chat"
              element={
                <RequireSession>
                  <Chat />
                </RequireSession>
              }
            />
            <Route
              path="/console"
              element={
                <RequireSession>
                  <Navigate to="/chat" replace />
                </RequireSession>
              }
            />
            <Route
              path="/workbench"
              element={
                <RequireSession>
                  <Navigate to="/chat" replace />
                </RequireSession>
              }
            />
            <Route
              path="/settings"
              element={
                <RequireSession>
                  <LocalSettingsAccess><Settings /></LocalSettingsAccess>
                </RequireSession>
              }
            />
            <Route path="/admin" element={<Navigate to="/console" replace />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
