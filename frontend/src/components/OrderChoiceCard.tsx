import { Check, MessageCircle, PackageCheck } from "lucide-react";
import { money } from "../lib/outcome";
import type { OrderChoice } from "../types";
import { Button } from "./ui";

function eligibilityText(order: OrderChoice): string {
  if (order.already_refunded) return "已退款";
  if (order.is_final_sale) return "特价商品 · 需先确认规则";
  if (order.status === "delivered") return "已签收 · 可继续核验";
  return "可继续核验";
}

export default function OrderChoiceCard({
  orders, status, selectedOrderId, disabled, onChoose, onContinue,
}: {
  orders: OrderChoice[];
  status: "pending" | "selected" | "skipped" | "abandoned";
  selectedOrderId?: string;
  disabled: boolean;
  onChoose: (order: OrderChoice) => void;
  onContinue: () => void;
}) {
  const selected = orders.find(order => order.order_id === selectedOrderId);
  const statusLabel = status === "pending" ? "等待选择" : status === "selected" ? `已选择${selected ? `：${selected.product_name}` : "订单"}` : status === "skipped" ? "已选择继续描述" : "你继续了新对话，此选择已关闭";
  return (
    <article className={`order-choice-card ${status !== "pending" ? "is-resolved" : ""}`} aria-label="选择要咨询的订单" data-card-status={status}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-2">
          {status === "pending" ? <PackageCheck size={17} className="mt-0.5 text-brand" /> : <Check size={17} className="mt-0.5 text-brand" />}
          <div>
            <strong className="text-sm">{status === "pending" ? "你想咨询哪一笔订单？" : "订单选择"}</strong>
            <p className="mt-1 text-xs leading-5 text-muted">{statusLabel}</p>
          </div>
        </div>
      </div>
      {status === "pending" && <>
        <p className="mt-1 text-xs leading-5 text-muted">选择订单只用于定位，不会直接退款；也可以继续用文字描述。</p>
        <div className="order-choice-list">
          {orders.map((order, index) => (
            <button key={order.order_id} type="button" className="order-choice" disabled={disabled} onClick={() => onChoose(order)}>
              <span className="min-w-0 text-left">
                <strong className="block truncate">{order.choice_label || (index < 26 ? String.fromCharCode(65 + index) : String(index + 1))} · {order.product_name}</strong>
                <small>{order.order_id} · {eligibilityText(order)}</small>
              </span>
              <span className="shrink-0 font-mono text-xs">{money(order.amount)}</span>
            </button>
          ))}
        </div>
        <Button size="sm" disabled={disabled} onClick={onContinue}><MessageCircle size={14} />继续描述，不选择订单</Button>
      </>}
    </article>
  );
}
