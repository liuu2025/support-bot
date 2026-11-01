"""Deterministic refund-policy engine.

This module is the single source of truth for refund eligibility. The LLM may
*propose* a refund, but only this code decides whether one is permitted. Tools
call `evaluate` before committing anything to the database, so a prompt-injected
or jailbroken model still cannot push through a non-compliant refund.

Pure functions, no LLM and no I/O — fully unit-testable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.config import get_settings


@dataclass
class PolicyResult:
    eligible: bool
    requires_escalation: bool
    reasons: list[str] = field(default_factory=list)
    refund_amount: float = 0.0

    @property
    def decision(self) -> str:
        """The decision the agent is permitted to record."""
        if not self.eligible:
            return "denied"
        if self.requires_escalation:
            return "escalated"
        return "approved"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decision"] = self.decision
        return d


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC for comparison."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate(order, customer=None, now: datetime | None = None) -> PolicyResult:
    """Evaluate whether `order` may be refunded.

    Rules are applied as hard gates in priority order. Ownership and delivery
    are validated first, then the policy rules from refund_policy_zh.md.
    """
    settings = get_settings()
    now = _now(now)
    reasons: list[str] = []
    amount = float(order.amount)  # Numeric → float for arithmetic and JSON

    # Rule 5 (identity/ownership): the order must belong to the requester.
    if customer is not None and order.customer_id != customer.id:
        reasons.append(
            f"订单 {order.id} 不属于当前请求的客户。"
        )
        return PolicyResult(False, False, reasons, amount)

    # The item must actually have been delivered before it can be returned.
    delivered = _as_aware(order.delivered_date)
    if order.status != "delivered" or delivered is None:
        reasons.append(
            f"订单 {order.id} 尚未签收，因此不符合退款条件。"
        )
        return PolicyResult(False, False, reasons, amount)

    # Rule 4: already refunded.
    if order.refunded:
        reasons.append(f"订单 {order.id} 已经退过款。")
        return PolicyResult(False, False, reasons, amount)

    # Rule 1: final-sale items are never refundable.
    if order.is_final_sale:
        reasons.append(
            f"「{order.product_name}」属于特价商品，任何情况下均不可退款。"
        )
        return PolicyResult(False, False, reasons, amount)

    # Rule 3: return window.
    age_days = (now - delivered).days
    if age_days > settings.return_window_days:
        reasons.append(
            f"订单 {order.id} 签收于 {age_days} 天前，已超出 "
            f"{settings.return_window_days} 天退货期。"
        )
        return PolicyResult(False, False, reasons, amount)

    # Rule 2: high-value refunds require a human.
    if amount > settings.escalation_threshold:
        reasons.append(
            f"退款金额 {amount:.2f} 元超过 "
            f"{settings.escalation_threshold:g} 元的自动审批上限，需要转人工审核。"
        )
        return PolicyResult(True, True, reasons, amount)

    reasons.append(
        f"订单 {order.id} 在 {settings.return_window_days} 天退货期内、非特价商品，"
        "且金额未超过人工审核上限，符合自动退款条件。"
    )
    return PolicyResult(True, False, reasons, amount)
