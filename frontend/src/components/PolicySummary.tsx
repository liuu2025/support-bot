import { Scale } from "lucide-react";
import { useMeta } from "../lib/meta";
import { money } from "../lib/outcome";

export default function PolicySummary() {
  const { meta } = useMeta();
  const policy = meta?.refund_policy;
  if (!policy) return null;
  return (
    <div className="policy-summary" aria-label="当前退款规则">
      <Scale size={15} />
      <strong>退款规则</strong>
      <span>签收 {policy.return_window_days} 天内</span>
      <i>·</i>
      <span>特价商品及已退款订单不可退</span>
      <i>·</i>
      <span>符合条件且超过 {money(policy.escalation_threshold)} 转人工</span>
      <small>以实际订单核验为准</small>
    </div>
  );
}
