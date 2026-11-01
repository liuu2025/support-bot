import { CheckCircle2, Clock3, FileCheck2, ShieldCheck } from "lucide-react";
import { money } from "../lib/outcome";
import type { RefundProposal } from "../types";
import { Button } from "./ui";

const LABELS: Record<RefundProposal["status"], string> = {
  pending: "等待你确认", confirmed: "已提交", cancelled: "你已取消本次申请",
  expired: "确认已过期，请重新发起", superseded: "已有新的问题或申请，此卡片已失效",
  invalidated: "订单或条件已变化，请重新核对",
};

export default function RefundConfirmation({ proposal, disabled, busy, onDecision }: {
  proposal: RefundProposal; disabled: boolean; busy: string | null;
  onDecision: (proposal: RefundProposal, decision: "confirm" | "cancel") => void;
}) {
  const expired = proposal.status === "pending" && Date.now() >= Date.parse(proposal.expires_at);
  const pending = proposal.status === "pending" && !expired;
  const review = proposal.action === "review";
  return <article className={`refund-confirmation ${pending ? "is-pending" : ""}`} aria-label={`退款确认 ${proposal.order_id}`}>
    <div className="flex items-center justify-between gap-3">
      <strong className="flex items-center gap-2 text-sm"><ShieldCheck size={17} />{pending ? "请核对后确认" : "申请处理记录"}</strong>
      <span className="text-xs text-muted">{expired ? LABELS.expired : LABELS[proposal.status]}</span>
    </div>
    <dl className="confirmation-fields">
      <div><dt>订单号</dt><dd className="font-mono">{proposal.order_id}</dd></div>
      <div><dt>商品</dt><dd>{proposal.product_name}</dd></div>
      <div><dt>{review ? "申请退款金额" : "本次退款金额"}</dt><dd className="text-lg font-semibold text-brand">{money(proposal.amount)}</dd></div>
      <div><dt>本次操作</dt><dd>{review ? "提交人工审核，等待处理" : "确认后记录退款并更新订单"}</dd></div>
    </dl>
    <p className="text-xs leading-6 text-muted">{proposal.reasons.join(" ")}</p>
    {pending ? <>
      <p className="mt-2 flex items-center gap-1.5 text-xs text-brand"><Clock3 size={13} />确认前不会退款{review ? "或提交人工工单" : ""}，此申请 15 分钟内有效。</p>
      <div className="mt-4 flex items-center gap-2">
        <Button variant="primary" disabled={disabled || Boolean(busy)} onClick={() => onDecision(proposal, "confirm")}><FileCheck2 size={15} />{busy === proposal.id ? "正在处理…" : review ? "确认提交人工审核" : "确认退款"}</Button>
        <Button disabled={disabled || Boolean(busy)} onClick={() => onDecision(proposal, "cancel")}>取消申请</Button>
      </div>
    </> : proposal.status === "confirmed" && <p className="mt-3 flex items-center gap-1.5 text-xs text-brand"><CheckCircle2 size={14} />{review ? "已创建人工审核工单，当前尚未退款。" : "已根据你的确认完成处理。"}</p>}
  </article>;
}
