import hmac
import json
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sse_starlette.sse import EventSourceResponse

from app import hitl, model_config, refund_confirmation, tracing
from app import redis as shared
from app.agent.graph import clear_graph_cache
from app.agent.llm import reset_model_cache
from app.agent.runner import (
    ConversationNotFound,
    create_conversation,
    load_owned_conversation,
    new_conversation_id,
    run_agent_turn,
)
from app.auth import (
    ROLE_ADMIN,
    ROLE_CUSTOMER,
    Principal,
    bearer_token,
    mint_token,
    principal_from_request,
    require_admin,
    require_customer,
)
from app.config import get_settings
from app.db import session as db
from app.db.models import Conversation, Customer, Message, Order, ReasoningEvent, Refund
from app.db.seed import seed_if_empty
from app.demo import order_out
from app.demo import router as demo_router
from app.events import broadcaster
from app.local_workspace import router as local_router
from app.observability import (
    REJECTIONS,
    SSE_STREAMS,
    RequestContextMiddleware,
    configure_logging,
    configure_sentry,
    customer_id_var,
    log_event,
)
from app.schemas import (
    AdminStats,
    ChatRequest,
    ConversationDetail,
    ConversationSummary,
    EscalationDetail,
    EscalationOut,
    MessageOut,
    ModelConfigUpdate,
    ModelListRequest,
    OrderChoiceDecision,
    OrderOut,
    PresetCreate,
    PresetRename,
    ReasoningEventOut,
    ResolveEscalationRequest,
)

settings = get_settings()
logger = logging.getLogger("app")

STREAM_ERROR_MESSAGE = "抱歉，处理你的消息时我们内部出错了，请稍后重试。"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    configure_sentry()
    tracing.configure()
    if settings.seed_on_startup:
        await seed_if_empty(create_tables=True)
    log_event(logger, "startup", env=settings.env)
    yield
    tracing.flush()
    await shared.close_redis()
    await db.dispose()


app = FastAPI(title="退款客服 API", lifespan=lifespan)
app.include_router(local_router)

# In Docker/k8s the SPA is same-origin (nginx/ingress route /api), so CORS is
# only needed when embedding the widget on another origin — allowlist those.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        # The UI reads these on 429/503 and for support references.
        expose_headers=["Retry-After", "X-Request-ID"],
        max_age=3600,
    )
app.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_request_bytes)
app.include_router(demo_router)


@app.exception_handler(Exception)
async def _unhandled(_request: Request, exc: Exception):
    log_event(
        logger,
        "unhandled error",
        level=logging.ERROR,
        error_type=type(exc).__name__,
        error=str(exc)[:500],
    )
    return JSONResponse(status_code=500, content={"detail": "internal error"})


# ---------------------------------------------------------------------------
# Health & metrics
# ---------------------------------------------------------------------------


@app.get("/api/health")
@app.get("/api/health/live")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/health/ready")
async def ready() -> Response:
    checks = {}
    try:
        async with db.SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:  # pragma: no cover - exercised in k8s
        checks["db"] = f"error: {type(exc).__name__}"
    try:
        await shared.get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["redis"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, **checks})


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    if settings.metrics_token:
        given = bearer_token(request) or ""
        if not hmac.compare_digest(given.encode(), settings.metrics_token.encode()):
            raise HTTPException(status_code=401, detail="metrics token required")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/meta")
async def meta() -> dict:
    """Public, non-sensitive facts the UI adapts to (sign-in flow, model)."""
    cfg = await model_config.effective_settings()
    return {
        "auth_mode": settings.auth_mode,
        "local_workspace": settings.env == "dev" and settings.auth_mode == "dev",
        # `model` is "openai-compatible" (the only protocol we speak) and
        # `model_name` is the actual id answering turns — null until an admin
        # saves a configuration, which the UI surfaces as "unconfigured".
        "model": "openai-compatible" if cfg.configured else "unconfigured",
        "model_name": cfg.model_name if cfg.configured else None,
        "refund_policy": {
            "return_window_days": settings.return_window_days,
            "escalation_threshold": settings.escalation_threshold,
        },
        "demo": {
            "session_ttl_s": settings.demo_token_ttl_s,
            "data_ttl_hours": settings.demo_data_ttl_hours,
            # Chats are archived for review when a sandbox is purged (maintenance/archive.py).
            "transcript_retention_days": settings.retention_days,
        }
        if settings.is_demo
        else None,
    }


# ---------------------------------------------------------------------------
# Dev-only token minting (AUTH_MODE=dev). In production this route 404s and
# tokens are issued by the host site.
# ---------------------------------------------------------------------------


class DevTokenRequest(BaseModel):
    customer_id: str | None = None
    admin: bool = False


@app.post("/api/dev/token")
async def dev_token(req: DevTokenRequest) -> dict:
    if settings.auth_mode != "dev" or settings.env == "prod":
        raise HTTPException(status_code=404, detail="Not Found")
    if req.admin:
        return {"token": mint_token("dev-admin", ROLE_ADMIN), "role": ROLE_ADMIN}
    if not req.customer_id:
        raise HTTPException(status_code=400, detail="customer_id is required")
    async with db.SessionLocal() as session:
        if await session.get(Customer, req.customer_id) is None:
            raise HTTPException(status_code=404, detail="unknown customer")
    return {"token": mint_token(req.customer_id, ROLE_CUSTOMER), "role": ROLE_CUSTOMER}


@app.get("/api/dev/customers")
async def dev_customers() -> list[dict]:
    """Customer picker for the local demo UI."""
    if settings.auth_mode != "dev" or settings.env == "prod":
        raise HTTPException(status_code=404, detail="Not Found")
    async with db.SessionLocal() as session:
        rows = (await session.scalars(select(Customer).order_by(Customer.id).limit(20))).all()
    return [{"id": c.id, "name": c.name, "email": c.email} for c in rows]


# ---------------------------------------------------------------------------
# Customer chat
# ---------------------------------------------------------------------------


def _rate_limit_key(request: Request) -> str:
    principal = principal_from_request(request)
    if principal is not None:
        return f"sub:{principal.sub}"
    # Uvicorn runs with --proxy-headers, so this is the real client IP when
    # the ingress/nginx is a trusted forwarder.
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def enforce_chat_rate_limit(request: Request) -> None:
    if principal_from_request(request) is None:
        # Missing/invalid token: let the auth dependency answer 401. Counting
        # these per IP would let one bad client behind a shared NAT/proxy
        # exhaust the limit for everyone else on that IP.
        return
    allowed, reset_in = await shared.hit_rate_limit(
        f"chat:{_rate_limit_key(request)}", settings.chat_rate_limit
    )
    if not allowed:
        REJECTIONS.labels("rate_limited").inc()
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {settings.chat_rate_limit}",
            headers={"Retry-After": str(reset_in)},
        )


@app.post("/api/chat", dependencies=[Depends(enforce_chat_rate_limit)])
async def chat(req: ChatRequest, principal: Principal = Depends(require_customer)):
    customer_id_var.set(principal.sub)
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    if req.order_id:
        async with db.SessionLocal() as session:
            owned = await session.scalar(select(Order.id).where(
                Order.id == req.order_id, Order.customer_id == principal.sub,
            ))
        if owned is None:
            raise HTTPException(404, "order not found")

    # Guardrail 1: per-message length cap.
    if len(req.message) > settings.max_message_chars:
        REJECTIONS.labels("too_long").inc()
        raise HTTPException(
            status_code=400,
            detail=(
                f"message is too long ({len(req.message)} chars); the limit is "
                f"{settings.max_message_chars}."
            ),
        )

    # Ownership: a client may only continue a conversation it owns. Unknown and
    # foreign ids are both 404 so they can't be probed.
    if req.conversation_id:
        try:
            await load_owned_conversation(principal.sub, req.conversation_id)
        except ConversationNotFound:
            raise HTTPException(status_code=404, detail="conversation not found") from None

        # Guardrail 2: per-conversation turn cap. Counted before the SSE opens
        # so the client gets a real HTTP status, not a half-stream.
        async with db.SessionLocal() as session:
            existing = (
                await session.scalar(
                    select(func.count(Message.id)).where(
                        Message.conversation_id == req.conversation_id
                    )
                )
                or 0
            )
        if existing >= settings.max_conversation_turns:
            REJECTIONS.labels("turn_cap").inc()
            raise HTTPException(
                status_code=429,
                detail=(
                    f"this conversation has reached its message limit "
                    f"({settings.max_conversation_turns}). Please start a new chat."
                ),
            )
        cid = req.conversation_id
        is_new = False
    else:
        cid = new_conversation_id()
        is_new = True

    # One turn at a time per conversation (across all pods).
    lock_ttl = settings.turn_timeout_s + 30
    lock = await shared.acquire_turn_lock(cid, lock_ttl)
    if lock is None:
        REJECTIONS.labels("turn_in_progress").inc()
        raise HTTPException(
            status_code=409, detail="a reply is still being generated for this conversation"
        )

    # Global back-pressure: shed load before the LLM provider starts 429ing.
    slot = await shared.acquire_turn_slot(settings.max_concurrent_turns, lock_ttl)
    if slot is None:
        await shared.release_turn_lock(cid, lock)
        REJECTIONS.labels("overloaded").inc()
        raise HTTPException(
            status_code=503,
            detail="We're experiencing high demand. Please try again shortly.",
            # Jittered so shed clients don't all come back in lockstep.
            headers={"Retry-After": str(random.randint(5, 15))},
        )

    if is_new:
        await create_conversation(principal.sub, cid)

    async def event_stream():
        SSE_STREAMS.labels("chat").inc()
        try:
            async for event in run_agent_turn(
                principal.sub, cid, req.message, selected_order_id=req.order_id,
                abandon_card_ids=req.abandon_card_ids,
            ):
                yield {"data": json.dumps(event)}
        except Exception as exc:
            # Headers are already sent, so an exception here would tear the
            # connection. End the stream cleanly with a generic error instead.
            log_event(
                logger,
                "chat stream failed",
                level=logging.ERROR,
                error_type=type(exc).__name__,
                error=str(exc)[:500],
            )
            yield {"data": json.dumps({"kind": "error", "message": STREAM_ERROR_MESSAGE})}
        finally:
            SSE_STREAMS.labels("chat").dec()
            await shared.release_turn_slot(slot)
            await shared.release_turn_lock(cid, lock)

    return EventSourceResponse(event_stream(), ping=15)


def _messages_out(messages) -> list[MessageOut]:
    return [
        MessageOut(id=m.id, role=m.role, content=m.content, created_at=m.created_at)
        for m in messages
    ]


@app.get("/api/me/conversations", response_model=list[ConversationSummary])
async def my_conversations(
    principal: Principal = Depends(require_customer),
    limit: int = Query(20, ge=1, le=100),
    before: datetime | None = None,
) -> list[ConversationSummary]:
    return await _conversation_summaries(limit, before, customer_id=principal.sub)


@app.get("/api/me/conversations/{conversation_id}", response_model=ConversationDetail)
async def my_conversation(
    conversation_id: str, principal: Principal = Depends(require_customer)
) -> ConversationDetail:
    try:
        convo = await load_owned_conversation(principal.sub, conversation_id)
    except ConversationNotFound:
        raise HTTPException(status_code=404, detail="conversation not found") from None
    async with db.SessionLocal() as session:
        messages = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id)
            )
        ).all()
        events = list(reversed((await session.scalars(select(ReasoningEvent).where(
            ReasoningEvent.conversation_id == conversation_id,
        ).order_by(ReasoningEvent.seq.desc()).limit(500))).all()))
    return ConversationDetail(
        id=convo.id,
        customer_id=convo.customer_id,
        customer_name=None,
        messages=_messages_out(messages),
        events=[ReasoningEventOut(id=e.id, message_id=e.message_id, seq=e.seq, step_type=e.step_type, node=e.node,
                                 payload=e.payload, created_at=e.created_at) for e in events],
    )


@app.get("/api/me/orders", response_model=list[OrderOut])
async def my_orders(principal: Principal = Depends(require_customer)) -> list[OrderOut]:
    async with db.SessionLocal() as session:
        orders = (
            await session.scalars(
                select(Order)
                .where(Order.customer_id == principal.sub)
                .order_by(Order.id)
                .limit(100)
            )
        ).all()
    return [order_out(o) for o in orders]


class ProposalDecision(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["confirm", "cancel"]


@app.get("/api/me/refund-proposals")
async def my_refund_proposals(conversation_id: str, principal: Principal = Depends(require_customer)):
    try:
        return await refund_confirmation.list_proposals(principal.sub, conversation_id)
    except refund_confirmation.ProposalError as exc:
        raise HTTPException(exc.status, exc.message) from None


@app.post("/api/me/order-choice-cards/{card_id}/decision")
async def decide_order_choice_card(
    card_id: str, body: OrderChoiceDecision, conversation_id: str, principal: Principal = Depends(require_customer),
):
    try:
        lock = await shared.acquire_turn_lock(conversation_id, 30)
        if lock is None:
            raise HTTPException(409, "当前任务正在处理，请稍后再操作。")
        try:
            return await refund_confirmation.set_choice_card_state(
                principal.sub, conversation_id, card_id, body.action, body.order_id,
            )
        finally:
            await shared.release_turn_lock(conversation_id, lock)
    except refund_confirmation.ProposalError as exc:
        raise HTTPException(exc.status, exc.message) from None


@app.post("/api/me/refund-proposals/{proposal_id}/decision")
async def decide_refund_proposal(
    proposal_id: str, body: ProposalDecision, principal: Principal = Depends(require_customer),
):
    try:
        proposal = await refund_confirmation.owned_proposal(principal.sub, proposal_id)
        cid = proposal["conversation_id"]
        lock = await shared.acquire_turn_lock(cid, 30)
        if lock is None:
            raise HTTPException(409, "当前任务正在处理，请稍后再操作。")
        try:
            return await refund_confirmation.act(principal.sub, proposal_id, body.decision)
        finally:
            await shared.release_turn_lock(cid, lock)
    except refund_confirmation.ProposalError as exc:
        raise HTTPException(exc.status, exc.message) from None


# ---------------------------------------------------------------------------
# Admin (requires role=admin). A scoped admin token (the public demo's) only
# sees its own customer's data; foreign ids are 404 so they can't be probed.
# ---------------------------------------------------------------------------


async def _conversation_summaries(
    limit: int, before: datetime | None, customer_id: str | None = None
) -> list[ConversationSummary]:
    msg_count = (
        select(func.count(Message.id))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )
    last_message = (
        select(Message.content)
        .where(Message.conversation_id == Conversation.id)
        .order_by(Message.id.desc())
        .limit(1)
        .correlate(Conversation)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation, Customer.name, msg_count, last_message)
        .outerjoin(Customer, Customer.id == Conversation.customer_id)
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.where(Conversation.created_at < before)
    if customer_id is not None:
        stmt = stmt.where(Conversation.customer_id == customer_id)
    async with db.SessionLocal() as session:
        rows = (await session.execute(stmt)).all()
    return [
        ConversationSummary(
            id=c.id,
            customer_id=c.customer_id,
            customer_name=name,
            created_at=c.created_at,
            message_count=count or 0,
            last_message=last,
        )
        for c, name, count, last in rows
    ]


@app.get("/api/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    limit: int = Query(50, ge=1, le=200),
    before: datetime | None = Query(None, description="cursor: created_at of last row"),
    principal: Principal = Depends(require_admin),
) -> list[ConversationSummary]:
    return await _conversation_summaries(limit, before, customer_id=principal.scope)


def _visible(convo: Conversation | None, principal: Principal) -> bool:
    return convo is not None and (principal.scope is None or convo.customer_id == principal.scope)


@app.get("/api/admin/stats", response_model=AdminStats)
async def admin_stats(principal: Principal = Depends(require_admin)) -> AdminStats:
    """Headline numbers for the dashboard (scoped like the conversation list)."""

    def scoped(stmt, conv_col):
        if principal.scope is None:
            return stmt
        owned = select(Conversation.id).where(Conversation.customer_id == principal.scope)
        return stmt.where(conv_col.in_(owned))

    async with db.SessionLocal() as session:
        conv_stmt = select(
            func.count(Conversation.id), func.coalesce(func.sum(Conversation.tokens_used), 0)
        )
        if principal.scope is not None:
            conv_stmt = conv_stmt.where(Conversation.customer_id == principal.scope)
        conversations, tokens = (await session.execute(conv_stmt)).one()
        messages = await session.scalar(
            scoped(select(func.count(Message.id)), Message.conversation_id)
        )
        decisions = (
            await session.execute(
                scoped(
                    select(Refund.decision, func.count(Refund.id)).group_by(Refund.decision),
                    Refund.conversation_id,
                )
            )
        ).all()
        flags = await session.scalar(
            scoped(
                select(func.count(ReasoningEvent.id)).where(
                    ReasoningEvent.step_type == "injection_flag"
                ),
                ReasoningEvent.conversation_id,
            )
        )
    queue = await hitl.stats()
    return AdminStats(
        conversations=conversations or 0,
        messages=messages or 0,
        decisions={d: n for d, n in decisions},
        injection_flags=flags or 0,
        tokens_used=int(tokens or 0),
        # A scoped token belongs to one customer's sandbox: the global review
        # queue is not its business, so it gets zeros instead of a leak.
        escalations_pending=0 if principal.scope else int(queue["pending"]),
        escalations_resolved=0 if principal.scope else int(queue["resolved"]),
        avg_resolution_seconds=None if principal.scope else queue["avg_resolution_seconds"],
    )


@app.get("/api/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str, principal: Principal = Depends(require_admin)
) -> ConversationDetail:
    async with db.SessionLocal() as session:
        convo = await session.get(Conversation, conversation_id)
        if not _visible(convo, principal):
            raise HTTPException(status_code=404, detail="conversation not found")
        name = None
        if convo.customer_id:
            cust = await session.get(Customer, convo.customer_id)
            name = cust.name if cust else None
        messages = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id)
            )
        ).all()
        events = (
            await session.scalars(
                select(ReasoningEvent)
                .where(ReasoningEvent.conversation_id == conversation_id)
                .order_by(ReasoningEvent.seq)
            )
        ).all()
    return ConversationDetail(
        id=convo.id,
        customer_id=convo.customer_id,
        customer_name=name,
        messages=_messages_out(messages),
        events=[
            ReasoningEventOut(
                id=e.id,
                message_id=e.message_id,
                seq=e.seq,
                step_type=e.step_type,
                node=e.node,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


@app.get("/api/conversations/{conversation_id}/stream")
async def stream_conversation(
    conversation_id: str, principal: Principal = Depends(require_admin)
):
    """Live reasoning events for a conversation (admin dashboard)."""
    if principal.scope is not None:
        async with db.SessionLocal() as session:
            convo = await session.get(Conversation, conversation_id)
        if not _visible(convo, principal):
            raise HTTPException(status_code=404, detail="conversation not found")

    async def event_stream():
        SSE_STREAMS.labels("admin").inc()
        try:
            async for event in broadcaster.subscribe(conversation_id):
                yield {"data": json.dumps(event)}
        finally:
            SSE_STREAMS.labels("admin").dec()

    return EventSourceResponse(event_stream(), ping=15)


@app.get("/api/me/conversations/{conversation_id}/stream")
async def stream_my_conversation(
    conversation_id: str, principal: Principal = Depends(require_customer)
):
    """Live events for the signed-in customer's own conversation.

    Lets an open chat page pick up a human reviewer's decision without a
    refresh, and keeps every other customer's stream unreachable (404, not 403,
    so ids cannot be probed).
    """
    try:
        await load_owned_conversation(principal.sub, conversation_id)
    except ConversationNotFound:
        raise HTTPException(status_code=404, detail="conversation not found") from None

    async def event_stream():
        SSE_STREAMS.labels("customer").inc()
        try:
            async for event in broadcaster.subscribe(conversation_id):
                yield {"data": json.dumps(event)}
        finally:
            SSE_STREAMS.labels("customer").dec()

    return EventSourceResponse(event_stream(), ping=15)


# ---------------------------------------------------------------------------
# Human-in-the-loop: the reviewer workbench.
#
# Only an unscoped admin token may act here. The public demo's scoped admin
# token is tied to one visitor's sandbox, so it can read its own tickets but
# must never resolve them — otherwise a visitor could approve their own refund.
# ---------------------------------------------------------------------------


async def require_reviewer(principal: Principal = Depends(require_admin)) -> Principal:
    if principal.scope is not None:
        raise HTTPException(
            status_code=403,
            detail="a customer-scoped token cannot act as a reviewer",
        )
    return principal


@app.get("/api/agent/escalations", response_model=list[EscalationOut])
async def reviewer_queue(
    status: str = Query("pending", pattern="^(pending|approved|denied|all)$"),
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_reviewer),
) -> list[EscalationOut]:
    """The queue of refunds waiting for a human (or a slice of history)."""
    return await hitl.list_escalations(status=None if status == "all" else status, limit=limit)


@app.get("/api/agent/escalations/stats")
async def reviewer_stats(principal: Principal = Depends(require_reviewer)) -> dict:
    return await hitl.stats()


@app.get("/api/agent/escalations/{ticket_id}", response_model=EscalationDetail)
async def reviewer_ticket(
    ticket_id: int, principal: Principal = Depends(require_reviewer)
) -> EscalationDetail:
    try:
        return await hitl.get_escalation(ticket_id)
    except hitl.EscalationNotFound:
        raise HTTPException(status_code=404, detail="escalation not found") from None


@app.post("/api/agent/escalations/{ticket_id}/resolve", response_model=EscalationDetail)
async def reviewer_resolve(
    ticket_id: int,
    req: ResolveEscalationRequest,
    principal: Principal = Depends(require_reviewer),
) -> EscalationDetail:
    """Approve or deny an escalated refund, then tell the customer."""
    try:
        return await hitl.resolve(
            ticket_id, req.decision, reviewer=principal.sub, note=req.note
        )
    except hitl.EscalationNotFound:
        raise HTTPException(status_code=404, detail="escalation not found") from None
    except hitl.EscalationAlreadyResolved as exc:
        raise HTTPException(
            status_code=409, detail=f"this escalation is already {exc.status}"
        ) from None
    except hitl.EscalationConflict:
        raise HTTPException(
            status_code=409, detail="an approved refund already exists for this order"
        ) from None


@app.get("/api/me/escalations", response_model=list[EscalationOut])
async def my_escalations(
    status: str = Query("all", pattern="^(pending|approved|denied|all)$"),
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(require_customer),
) -> list[EscalationOut]:
    """The signed-in customer's own escalated requests, newest first."""
    return await hitl.list_escalations(
        status=None if status == "all" else status,
        limit=limit,
        customer_id=principal.sub,
    )


# ---------------------------------------------------------------------------
# Model settings (OpenAI-compatible endpoint).
#
# Admin-only and unscoped, like the reviewer workbench: this decides which
# service the whole agent runs on, so a demo visitor's sandbox token must never
# reach it. The API key is written once and never read back.
# ---------------------------------------------------------------------------


def _model_error(exc: model_config.ModelConfigError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@app.get("/api/admin/model-config")
async def get_model_config(principal: Principal = Depends(require_reviewer)) -> dict:
    """Current endpoint (no key), so the settings page can render itself."""
    return model_config.public_config(await model_config.effective_settings())


@app.put("/api/admin/model-config")
async def put_model_config(
    req: ModelConfigUpdate, principal: Principal = Depends(require_reviewer)
) -> dict:
    try:
        saved = await model_config.save_config(
            req.base_url, req.model_name, req.api_key, req.timeout_s, principal.sub
        )
    except model_config.ModelConfigError as exc:
        raise _model_error(exc) from None
    # Drop this process's cached clients/graph so the next turn uses the new
    # endpoint. Other replicas pick it up on their next turn via the version.
    reset_model_cache()
    clear_graph_cache()
    log_event(
        logger,
        "model config saved",
        model=saved.model_name,
        base_url=saved.base_url,
        version=saved.version,
        by=principal.sub,
    )
    return model_config.public_config(saved)


@app.delete("/api/admin/model-config", status_code=204)
async def delete_model_config(principal: Principal = Depends(require_reviewer)) -> Response:
    await model_config.clear_config()
    reset_model_cache()
    clear_graph_cache()
    return Response(status_code=204)


@app.post("/api/admin/model-config/models")
async def list_model_options(
    req: ModelListRequest, principal: Principal = Depends(require_reviewer)
) -> dict:
    """Model ids the endpoint advertises (`GET {base}/models`) for the dropdown."""
    try:
        current = await model_config.effective_settings()
        api_key = model_config.resolve_api_key(req.base_url, req.api_key, current)
        models = await model_config.list_models(req.base_url, api_key)
    except model_config.ModelConfigError as exc:
        raise _model_error(exc) from None
    return {"models": models}


@app.post("/api/admin/model-config/test")
async def test_model_config(principal: Principal = Depends(require_reviewer)) -> dict:
    """One real tool-calling round trip against the saved configuration."""
    try:
        return await model_config.probe(await model_config.effective_settings())
    except model_config.ModelConfigError as exc:
        raise _model_error(exc) from None


@app.get("/api/admin/model-config/presets")
async def get_presets(principal: Principal = Depends(require_reviewer)) -> list[dict]:
    return await model_config.list_presets()


@app.post("/api/admin/model-config/presets")
async def create_preset(
    req: PresetCreate, principal: Principal = Depends(require_reviewer)
) -> dict:
    try:
        return await model_config.save_preset(
            req.name, req.base_url, req.model_name, req.api_key, req.timeout_s, principal.sub
        )
    except model_config.ModelConfigError as exc:
        raise _model_error(exc) from None


@app.patch("/api/admin/model-config/presets/{preset_id}")
async def rename_preset(
    preset_id: int, req: PresetRename, principal: Principal = Depends(require_reviewer)
) -> dict:
    try:
        return await model_config.rename_preset(preset_id, req.name)
    except model_config.ModelConfigError as exc:
        raise _model_error(exc) from None


@app.delete("/api/admin/model-config/presets/{preset_id}", status_code=204)
async def remove_preset(
    preset_id: int, principal: Principal = Depends(require_reviewer)
) -> Response:
    try:
        await model_config.delete_preset(preset_id)
    except model_config.ModelConfigError as exc:
        raise _model_error(exc) from None
    return Response(status_code=204)


@app.post("/api/admin/model-config/presets/{preset_id}/apply")
async def apply_preset_endpoint(
    preset_id: int, principal: Principal = Depends(require_reviewer)
) -> dict:
    try:
        saved = await model_config.apply_preset(preset_id, principal.sub)
    except model_config.ModelConfigError as exc:
        raise _model_error(exc) from None
    reset_model_cache()
    clear_graph_cache()
    return model_config.public_config(saved)
