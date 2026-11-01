import { Activity } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { OUTCOME_META, outcomeOf } from "../lib/outcome";
import type { RefundProposal, StepEvent } from "../types";
import ReasoningTimeline from "./ReasoningTimeline";
import { Badge, LiveDot } from "./ui";

const EVENT_VIEW_KEY = "zhixu.trace.showAllEvents";
const DETAIL_EVENTS = new Set(["model", "usage", "trace"]);

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg bg-surface-2 px-2.5 py-2">
      <div className="text-[10px] font-medium uppercase tracking-wide text-subtle">{label}</div>
      <div className="mt-0.5 font-mono text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

/** Live view of the current (or last) agent turn: stats plus the step timeline. */
export default function AgentPanel({
  steps,
  streaming,
  startedAt,
  endedAt,
  proposals = [],
  showTitle = true,
}: {
  steps: StepEvent[];
  streaming: boolean;
  startedAt: number | null;
  endedAt: number | null;
  proposals?: RefundProposal[];
  showTitle?: boolean;
}) {
  const [now, setNow] = useState(Date.now());
  const [showAllEvents, setShowAllEvents] = useState(() => {
    try { return localStorage.getItem(EVENT_VIEW_KEY) === "true"; }
    catch { return false; }
  });
  const eventViewHelpId = useId();
  const endRef = useRef<HTMLDivElement>(null);

  function changeEventView(showAll: boolean) {
    setShowAllEvents(showAll);
    try { localStorage.setItem(EVENT_VIEW_KEY, String(showAll)); }
    catch { /* The view still switches when browser storage is unavailable. */ }
  }

  useEffect(() => {
    if (!streaming) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [streaming]);

  useEffect(() => {
    const container = endRef.current?.parentElement;
    container?.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
  }, [steps.length]);

  const tokens = steps
    .filter((s) => s.step_type === "usage")
    .reduce((n, s) => n + (s.payload.input_tokens ?? 0) + (s.payload.output_tokens ?? 0), 0);
  const tools = steps.filter((s) => s.step_type === "tool_call").length;
  const elapsed = startedAt ? ((streaming ? now : endedAt ?? now) - startedAt) / 1000 : 0;
  const outcome = outcomeOf(steps, proposals);
  const visibleSteps = showAllEvents ? steps : steps.filter(s => !DETAIL_EVENTS.has(s.step_type));

  return (
    <div className="flex h-full flex-col">
      <div className="space-y-3 border-b border-line p-4">
        <div className="flex items-center justify-between">
          {showTitle ? (
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Activity size={16} className="text-brand" /> Agent 执行记录
            </div>
          ) : (
            <span className="text-xs text-subtle">最近一轮</span>
          )}
          {streaming ? (
            <span className="flex items-center gap-1.5 text-[11px] font-semibold text-bad">
              <LiveDot /> 进行中
            </span>
          ) : outcome ? (
            <Badge tone={OUTCOME_META[outcome].tone}>{OUTCOME_META[outcome].label}</Badge>
          ) : null}
        </div>
        <p className="text-xs leading-6 text-muted">查看实际查询、规则校验与操作结果。准备申请后，Agent 会停下等待你的确认。</p>
        <div className="space-y-1.5">
          <label className="flex cursor-pointer items-center justify-between gap-3 text-xs text-muted">
            <span>显示全部事件</span>
            <input type="checkbox" checked={showAllEvents} aria-describedby={eventViewHelpId}
              onChange={event => changeEventView(event.target.checked)} className="h-3.5 w-3.5 accent-brand" />
          </label>
          <p id={eventViewHelpId} className="text-[11px] leading-5 text-subtle">{showAllEvents
            ? "已包含模型输出、Token 用量与调用链，按原始编号展示。"
            : "仅展示工具与业务结果；隐藏其他事件时，原始编号会跳号。"}</p>
        </div>
        <details className="text-xs text-subtle"><summary className="cursor-pointer">运行统计</summary><div className="mt-3 grid grid-cols-3 gap-2">
          <Stat label="工具调用" value={tools} />
          <Stat label="Token" value={tokens.toLocaleString()} />
          <Stat label="耗时" value={`${elapsed.toFixed(1)}s`} />
        </div></details>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {steps.length === 0 && !streaming ? (
          <div className="px-2 py-10 text-center">
            <p className="text-sm font-medium">任务开始后，过程会显示在这里</p>
            <p className="mt-1 text-xs text-subtle">
              查询订单 → 核验条件 → 准备申请 → 等待确认 → 执行结果
            </p>
          </div>
        ) : (
          <ReasoningTimeline steps={visibleSteps} />
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
