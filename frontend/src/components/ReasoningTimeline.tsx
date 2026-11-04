import clsx from "clsx";
import { Fragment } from "react";
import {
  AlertTriangle,
  Brain,
  ReceiptText,
  ExternalLink,
  Gauge,
  Info,
  MessageCircle,
  ShieldAlert,
  ShieldCheck,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { ChatMessage, Outcome, StepEvent } from "../types";
import { DECISION_LABEL, OUTCOME_META, money, type Tone } from "../lib/outcome";
import Markdown from "./Markdown";
import { Badge } from "./ui";

const STEP_META: Record<
  string,
  { label: string; icon: LucideIcon; tone: Tone }
> = {
  model: { label: "模型输出", icon: Brain, tone: "neutral" },
  tool_call: { label: "调用工具", icon: Wrench, tone: "info" },
  tool_result: { label: "工具返回", icon: Wrench, tone: "neutral" },
  policy_eval: { label: "规则校验", icon: ShieldCheck, tone: "warn" },
  decision: { label: "写入决策", icon: ReceiptText, tone: "ok" },
  proposal: { label: "准备待确认申请", icon: ReceiptText, tone: "warn" },
  confirmation: { label: "用户选择与执行结果", icon: ShieldCheck, tone: "ok" },
  injection_flag: { label: "注入尝试", icon: ShieldAlert, tone: "bad" },
  error: { label: "错误", icon: AlertTriangle, tone: "bad" },
  usage: { label: "Token 用量", icon: Gauge, tone: "neutral" },
  budget_exhausted: { label: "额度用尽", icon: Gauge, tone: "warn" },
  output_correction: { label: "输出已纠正", icon: ShieldAlert, tone: "bad" },
  notice: { label: "提示", icon: Info, tone: "info" },
  turn_summary: { label: "本轮统计", icon: Gauge, tone: "neutral" },
  trace: { label: "调用链", icon: ExternalLink, tone: "brand" },
};

const DOT: Record<Tone, string> = {
  ok: "bg-ok/15 text-ok",
  bad: "bg-bad/15 text-bad",
  warn: "bg-warn/15 text-warn",
  info: "bg-info/15 text-info",
  brand: "bg-brand/15 text-brand",
  neutral: "bg-surface-2 text-muted",
};

function Json({ value, label = "详情" }: { value: unknown; label?: string }) {
  return (
    <details className="group mt-1.5">
      <summary className="cursor-pointer select-none text-[11px] font-medium text-subtle hover:text-muted">
        {label}
      </summary>
      <pre className="mt-1 max-h-56 overflow-auto rounded-md bg-surface-2 p-2 font-mono text-[11px] leading-relaxed text-muted">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

export function DecisionBadge({ decision }: { decision?: string }) {
  if (!decision) return null;
  const meta = OUTCOME_META[decision as Outcome];
  return (
    <Badge tone={meta?.tone ?? "neutral"}>
      {DECISION_LABEL[decision] ?? decision.toUpperCase()}
    </Badge>
  );
}

function argsSummary(args: Record<string, unknown> | undefined): string {
  if (!args) return "";
  return Object.entries(args)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(", ");
}

function StepBody({ step }: { step: StepEvent }) {
  const p = step.payload || {};
  switch (step.step_type) {
    case "model":
      return (
        <div className="text-[13px] text-fg/90">
          <Markdown>{p.text ?? ""}</Markdown>
        </div>
      );
    case "tool_call":
      return (
        <div className="text-xs leading-6 text-muted">
          <strong>{({ get_order: "查询订单详情", list_orders: "查询当前客户的订单", offer_order_choices: "展示可选订单", get_my_profile: "读取当前客户资料", check_refund_eligibility: "核验退款条件", prepare_refund: "准备待确认方案" } as Record<string, string>)[p.tool] || p.tool}</strong>
          {p.args?.order_id && <p className="break-all font-mono text-[11px]">{p.args.order_id}</p>}
          <details className="mt-1 text-subtle"><summary className="cursor-pointer">工具参数</summary><code className="break-all">{p.tool}({argsSummary(p.args)})</code></details>
        </div>
      );
    case "policy_eval":
    case "decision": {
      const r = p.result || {};
      return (
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-xs text-muted">{p.tool}</code>
            {step.step_type === "policy_eval" ? <Badge tone={r.decision === "denied" ? "bad" : "info"}>{({ approved: "符合退款条件", denied: "不符合退款条件", escalated: "需人工审核" } as Record<string, string>)[r.decision] || "已核验"}</Badge> : <DecisionBadge decision={r.decision} />}
            {typeof r.amount === "number" && (
              <span className="text-xs text-muted">{money(r.amount)}</span>
            )}
          </div>
          {Array.isArray(r.reasons) && r.reasons.length > 0 && (
            <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-xs text-muted">
              {r.reasons.map((reason: string, i: number) => (
                <li key={i}>{reason}</li>
              ))}
            </ul>
          )}
        </div>
      );
    }
    case "proposal": {
      const result = p.result || {};
      return <div className="text-xs leading-6 text-muted">
        {result.proposal ? <><p>订单 {result.proposal.order_id}</p><p>{money(result.proposal.amount)} · {result.proposal.action === "review" ? "需要人工审核" : "符合自动退款条件"}</p><p className="text-brand">等待客户点击确认，尚未执行操作。</p></> : <p>{result.message || result.reasons?.join(" ") || "无法生成申请，请查看订单或规则结果。"}</p>}
      </div>;
    }
    case "confirmation":
      return <p className="text-xs leading-6 text-muted">订单 {p.order_id}<br />{({ confirmed: p.result?.decision === "escalated" ? "客户确认后，人工工单已创建；尚未退款。" : "客户确认后，后端复核通过，退款已记录。", cancelled: "客户取消，订单状态未改变。", invalidated: "订单或条件变化，未执行旧申请。", expired: "申请已过期，未执行。" } as Record<string, string>)[p.status] || "申请状态已更新。"}</p>;
    case "tool_result": {
      const r = p.result || {};
      return (
        <div>
          <code className="font-mono text-xs text-muted">{p.tool}</code>
          {r.error && (
            <Badge tone="bad" className="ml-2">
              {String(r.error)}
            </Badge>
          )}
          <Json value={r} label="返回结果" />
        </div>
      );
    }
    case "injection_flag":
      return (
        <p className="text-xs text-bad">
          命中特征：
          <span className="font-mono">{(p.patterns || []).join(", ")}</span>
          。最终结果仍由代码规则决定。
        </p>
      );
    case "usage":
      return (
        <p className="font-mono text-[11px] text-subtle">
          输入 {p.input_tokens} · 输出 {p.output_tokens}
          {p.model ? ` · ${p.model}` : ""}
        </p>
      );
    case "turn_summary":
      return <p className="text-xs leading-6 text-muted">本轮：工具 {p.tool_calls ?? 0} 次 · Token {((p.input_tokens ?? 0) + (p.output_tokens ?? 0)).toLocaleString()} · 耗时 {Number(p.elapsed_seconds ?? 0).toFixed(1)}s</p>;
    case "notice":
      return <p className="text-xs text-info">{p.message}</p>;
    case "output_correction":
      return <p className="text-xs text-bad">{p.reason}</p>;
    case "error":
      return (
        <p className="text-xs text-bad">模型调用失败（{p.error_type}）。</p>
      );
    case "trace":
      return p.url ? (
        <a
          href={p.url}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
        >
          在 Opik 中查看 <ExternalLink size={12} />
        </a>
      ) : (
        <code className="font-mono text-[11px] text-subtle">{p.trace_id}</code>
      );
    default:
      return <Json value={p} />;
  }
}

export default function ReasoningTimeline({
  steps,
  messages,
}: {
  steps: StepEvent[];
  messages: ChatMessage[];
}) {
  if (steps.length === 0) {
    return (
      <p className="px-1 py-8 text-center text-sm text-subtle">
        暂无执行步骤。
      </p>
    );
  }
  const userMessages = new Map(messages.filter(message => message.role === "user").map(message => [message.id, message]));
  const shownQuestions = new Set<string>();

  return (
    <ol className="relative space-y-3 before:absolute before:bottom-2 before:left-[13px] before:top-2 before:w-px before:bg-line">
      {steps.map((step) => {
        const meta = STEP_META[step.step_type] || STEP_META.tool_result;
        const Icon = meta.icon;
        const question = step.message_id == null ? undefined : userMessages.get(String(step.message_id));
        const showQuestion = Boolean(question && !shownQuestions.has(question.id));
        if (showQuestion && question) shownQuestions.add(question.id);
        return (
          <Fragment key={`${step.id}-${step.seq}`}>
            {showQuestion && question && <li className="trace-question relative flex animate-fade-up gap-3" aria-label={`用户问题：${question.content}`}>
              <span className="relative z-10 mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand/10 text-brand ring-4 ring-surface">
                <MessageCircle size={14} />
              </span>
              <div className="min-w-0 flex-1 rounded-md border border-brand/15 bg-brand/5 px-3 py-2.5">
                <span className="text-[10px] font-semibold text-brand">用户问题</span>
                <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-5 text-fg">{question.content}</p>
              </div>
            </li>}
            <li className="trace-step relative flex animate-fade-up gap-3">
              <span
                className={clsx(
                  "relative z-10 mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ring-4 ring-surface",
                  DOT[meta.tone],
                )}
              >
                <Icon size={14} />
              </span>
              <div className="min-w-0 flex-1 pb-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-subtle">
                    {meta.label}
                  </span>
                  <span className="font-mono text-[10px] text-subtle">
                    #{step.seq}
                  </span>
                </div>
                <div className="mt-0.5">
                  <StepBody step={step} />
                </div>
              </div>
            </li>
          </Fragment>
        );
      })}
    </ol>
  );
}
