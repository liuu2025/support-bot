from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    order_id: str | None = None
    abandon_card_ids: list[str] = Field(default_factory=list, max_length=100)


class OrderChoiceDecision(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["selected", "skipped"]
    order_id: str | None = None


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime


class ReasoningEventOut(BaseModel):
    id: int
    message_id: int | None = None
    seq: int
    step_type: str
    node: str
    payload: dict
    created_at: datetime


class ConversationSummary(BaseModel):
    id: str
    customer_id: str | None
    customer_name: str | None
    created_at: datetime
    message_count: int
    last_message: str | None


class ConversationDetail(BaseModel):
    id: str
    customer_id: str | None
    customer_name: str | None
    messages: list[MessageOut]
    events: list[ReasoningEventOut]


class OrderOut(BaseModel):
    id: str
    product_name: str
    amount: float
    status: str
    order_date: datetime | None
    delivered_date: datetime | None
    is_final_sale: bool
    refunded: bool
    # Which refund-policy branch a demo/fixture order exercises, if any.
    scenario: str | None = None


class AdminStats(BaseModel):
    conversations: int
    messages: int
    decisions: dict[str, int]
    injection_flags: int
    tokens_used: int
    # Human-in-the-loop queue
    escalations_pending: int = 0
    escalations_resolved: int = 0
    avg_resolution_seconds: float | None = None


class EscalationOut(BaseModel):
    """One escalated refund as the reviewer queue (or the customer) sees it."""

    id: int
    order_id: str
    product_name: str
    amount: float
    status: str  # pending | approved | denied
    decision: str  # escalated while pending, then approved | denied
    reason: str
    customer_id: str | None
    customer_name: str | None
    conversation_id: str | None
    escalated_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    resolution_note: str
    waiting_seconds: float | None
    handled_seconds: float | None


class EscalationDetail(EscalationOut):
    transcript: list[dict] = []


class ResolveEscalationRequest(BaseModel):
    decision: Literal["approved", "denied"]
    note: str = ""


# --- Model settings (OpenAI-compatible endpoint, edited in the UI) -----------


class ModelConfigUpdate(BaseModel):
    base_url: str
    model_name: str
    # Empty keeps the stored key, so the UI can show a masked field.
    api_key: str = ""
    timeout_s: int = 60


class ModelListRequest(BaseModel):
    base_url: str
    api_key: str = ""


class PresetCreate(BaseModel):
    name: str
    base_url: str
    model_name: str
    api_key: str = ""
    timeout_s: int = 60


class PresetRename(BaseModel):
    name: str
