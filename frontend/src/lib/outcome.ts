import type { Outcome, RefundProposal, StepEvent } from "../types";

// Tool errors that mean "not your order" (unknown ids look the same on purpose).
const BLOCKING_ERRORS = new Set([
  "ownership_mismatch",
  "identity_not_verified",
  "order_not_found",
]);

/** Keep eligibility, customer authorization and completed actions distinct. */
export function outcomeOf(steps: StepEvent[], proposals: RefundProposal[] = []): Outcome | null {
  let outcome: Outcome | null = null;
  const decisions: Record<string, Outcome> = {
    approved: "approved", denied: "denied", escalated: "escalated",
    cancelled: "cancelled", not_executed: "not_executed",
  };
  for (const s of steps) {
    const r = s.payload?.result;
    if (s.step_type === "policy_eval" && r?.decision) {
      outcome = r.decision === "approved" ? "eligible" : r.decision === "escalated" ? "review_required" : "denied";
    } else if (s.step_type === "proposal" && r?.proposal) {
      const proposal = proposals.find(p => p.id === r.proposal.id) || r.proposal;
      const expired = Date.parse(proposal.expires_at) <= Date.now();
      outcome = proposal.status === "pending" && !expired ? "awaiting_confirmation" :
        proposal.status === "cancelled" ? "cancelled" : proposal.status === "confirmed" ?
          (proposal.action === "review" ? "escalated" : "approved") : "not_executed";
    } else if ((s.step_type === "confirmation" || s.step_type === "decision") && decisions[r?.decision]) {
      outcome = decisions[r.decision];
    }
    if (BLOCKING_ERRORS.has(r?.error)) outcome = "blocked";
    if (!outcome && s.step_type === "injection_flag") outcome = "flagged";
  }
  return outcome;
}

export const OUTCOME_META: Record<Outcome, { label: string; tone: Tone }> = {
  eligible: { label: "符合退款条件", tone: "info" },
  review_required: { label: "需人工审核", tone: "warn" },
  awaiting_confirmation: { label: "等待客户确认", tone: "warn" },
  approved: { label: "退款已记录", tone: "ok" },
  denied: { label: "不符合退款条件", tone: "bad" },
  escalated: { label: "人工工单已提交", tone: "warn" },
  cancelled: { label: "已取消申请", tone: "neutral" },
  not_executed: { label: "申请未执行", tone: "neutral" },
  blocked: { label: "已拦截（越权）", tone: "bad" },
  flagged: { label: "检测到注入", tone: "warn" },
};

/** Short label for a raw decision code, used in the reasoning timeline. */
export const DECISION_LABEL: Record<string, string> = {
  approved: "批准",
  denied: "拒绝",
  escalated: "转人工",
};

export type Tone = "ok" | "bad" | "warn" | "info" | "brand" | "neutral";

export const money = (n: number) =>
  `${n.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 2 })} 元`;
