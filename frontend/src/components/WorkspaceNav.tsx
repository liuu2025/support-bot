import { Bot, MessageSquare, Moon, Settings2, Sun, UserRound } from "lucide-react";
import { NavLink } from "react-router-dom";
import { useMeta } from "../lib/meta";
import { useWorkspaceSession } from "../lib/workspaceSession";
import { useTheme } from "../theme";
import Logo from "./Logo";
import { Badge, IconButton } from "./ui";

export function WorkspaceTopbar() {
  const { meta } = useMeta();
  const { session } = useWorkspaceSession();
  const [theme, toggleTheme] = useTheme();
  return <header className="workspace-topbar">
    <NavLink to="/chat" className="flex items-center gap-3 text-fg">
      <Logo size={34} /><span><strong className="block text-base">退款客服</strong><small className="text-[10px] text-subtle">退款 Agent 工作台</small></span>
    </NavLink>
    <nav aria-label="主导航" className="workspace-header-nav">
      <NavLink to="/chat"><MessageSquare size={15} />退款工作台</NavLink>
      <NavLink to="/settings"><Settings2 size={15} />模型设置</NavLink>
    </nav>
    <div className="flex items-center gap-4">
      {session && <span className="flex items-center gap-2 text-xs text-muted"><UserRound size={15} />{session.customer.name}<span className="text-subtle">· 演示客户</span></span>}
      <Badge tone={meta?.model === "unconfigured" ? "warn" : "brand"}><Bot size={12} />{meta?.model === "unconfigured" ? "待配置模型" : meta?.model_name || "模型服务"}</Badge>
      <IconButton label={theme === "dark" ? "切换浅色主题" : "切换深色主题"} onClick={toggleTheme}>{theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}</IconButton>
    </div>
  </header>;
}
