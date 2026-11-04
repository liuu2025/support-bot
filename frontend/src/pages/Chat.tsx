import clsx from "clsx";
import { ArrowUp, CheckCircle2, Clock3, Columns2, LayoutPanelLeft, Package, RefreshCw, UserRound } from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, decideOrderChoiceCard, decideRefundProposal, fetchMyConversation, fetchMyEscalations, fetchOrders, fetchRefundProposals, friendlyError, streamChat, subscribeToMyConversation } from "../api";
import { getToken } from "../auth";
import AgentPanel from "../components/AgentPanel";
import Logo from "../components/Logo";
import Markdown from "../components/Markdown";
import PolicySummary from "../components/PolicySummary";
import RefundConfirmation from "../components/RefundConfirmation";
import OrderChoiceCard from "../components/OrderChoiceCard";
import { useToast } from "../components/Toaster";
import { Button, Spinner } from "../components/ui";
import { money } from "../lib/outcome";
import { useWorkspaceSession } from "../lib/workspaceSession";
import type { AgentEvent, CardStatusEvent, ChatMessage, Escalation, Order, OrderChoice, RefundProposal, StepEvent } from "../types";

interface ChoiceCardView {
  cardId: string;
  sourceMessageId: number;
  anchorMessageId: string;
  orders: OrderChoice[];
  status: "pending" | "selected" | "skipped" | "abandoned";
  selectedOrderId?: string;
}

function parseOrderChoices(value: unknown): OrderChoice[] {
  if (!Array.isArray(value)) return [];
  return value.filter((row): row is OrderChoice => Boolean(row && typeof row === "object"
    && typeof row.order_id === "string" && typeof row.product_name === "string"
    && typeof row.amount === "number" && typeof row.status === "string"
    && typeof row.is_final_sale === "boolean" && typeof row.already_refunded === "boolean"));
}

function choiceLabel(order: OrderChoice, index: number): string {
  return String(order.choice_label || (index < 26 ? String.fromCharCode(65 + index) : String(index + 1))).toLocaleLowerCase();
}

const SCENARIOS: Record<string, { label: string; detail: string }> = {
  refundable: { label: "正常退款", detail: "签收期内 · 非特价" },
  final_sale: { label: "特价不可退", detail: "查看 Agent 如何说明规则" },
  high_value: { label: "大额转人工", detail: "超出自动审批金额" },
};

export default function Chat() {
  const { session, restart } = useWorkspaceSession();
  const toast = useToast();
  const customerId = session?.customer.id || "host";
  const storageKey = `zhixu.conversation.${customerId}`;
  const orderKey = `zhixu.selectedOrder.${customerId}`;
  const [orders, setOrders] = useState<Order[]>(session?.orders || []);
  const [selected, setSelected] = useState(() => sessionStorage.getItem(orderKey) || "");
  const [hasExplicitOrderSelection, setHasExplicitOrderSelection] = useState(() => Boolean(sessionStorage.getItem(orderKey)));
  useEffect(() => {
    const storedOrder = sessionStorage.getItem(orderKey) || "";
    setSelected(storedOrder);
    setHasExplicitOrderSelection(Boolean(storedOrder));
  }, [orderKey]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const cidRef = useRef<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [turnMessageId, setTurnMessageId] = useState<number | null>(null);
  const [proposals, setProposals] = useState<RefundProposal[]>([]);
  const [cardStates, setCardStates] = useState<Record<string, CardStatusEvent>>({});
  const layoutKey = "zhixu.orderLayout";
  const [orderLayout, setOrderLayout] = useState<"top" | "left">(() => sessionStorage.getItem(layoutKey) === "top" ? "top" : "left");
  const [tickets, setTickets] = useState<Escalation[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const streamingRef = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [endedAt, setEndedAt] = useState<number | null>(null);
  /** Explicitly chosen in the optional card; null means continue without one. */
  const [choiceOrderOverride, setChoiceOrderOverride] = useState<string | null | undefined>(undefined);
  const optimisticUserId = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const currentOrder = orders.find(order => order.id === selected) || orders[0];
  const ticket = tickets.find(t => t.order_id === currentOrder?.id);
  const choiceCards = useMemo(() => {
    const cards = new Map<number, ChoiceCardView>();
    const userMessageAt = (createdAt: string) => {
      const timestamp = Date.parse(createdAt);
      if (!Number.isFinite(timestamp)) return undefined;
      return [...messages].reverse().find(message => message.role === "user" && message.at <= timestamp);
    };
    for (const step of steps) {
      if (step.step_type !== "tool_result" || step.payload.tool !== "offer_order_choices") continue;
      const sourceMessage = step.message_id == null ? userMessageAt(step.created_at) : undefined;
      const sourceId = step.message_id ?? (sourceMessage ? Number(sourceMessage.id) : NaN);
      if (!Number.isSafeInteger(sourceId)) continue;
      const cardId = `choice:${sourceId}`;
      const state = cardStates[cardId];
      const choiceStatus = state?.card_type === "order_choice" &&
        (state.status === "pending" || state.status === "selected" || state.status === "skipped" || state.status === "abandoned")
        ? state.status : "pending";
      cards.set(sourceId, {
        cardId, sourceMessageId: sourceId, anchorMessageId: String(sourceId),
        orders: parseOrderChoices(step.payload.result?.orders),
        // A card is pending until the server has recorded a terminal state.
        // A later user turn is not itself proof of abandonment: unrelated
        // pending cards are intentionally allowed to coexist.
        status: choiceStatus, selectedOrderId: state?.order_id,
      });
    }
    return cards;
  }, [steps, cardStates, messages]);
  const cardsAfterMessage = useMemo(() => {
    const cards = new Map<string, ChoiceCardView[]>();
    const messageIds = new Set(messages.map(message => message.id));
    for (const card of choiceCards.values()) {
      const source = String(card.sourceMessageId);
      if (!messageIds.has(source)) continue;
      const anchor = source;
      const anchored = { ...card, anchorMessageId: anchor };
      cards.set(anchor, [...(cards.get(anchor) || []), anchored]);
    }
    return cards;
  }, [choiceCards, messages]);
  const proposalAfterMessage = useMemo(() => {
    const map = new Map<string, RefundProposal[]>();
    const messageIds = new Set(messages.map(message => message.id));
    const anchorForMissingSource = (createdAt: string) => {
      const timestamp = Date.parse(createdAt);
      if (!Number.isFinite(timestamp)) return undefined;
      return [...messages].reverse().find(message => message.role === "user" && message.at <= timestamp)?.id;
    };
    for (const proposal of proposals) {
      const source = proposal.source_message_id == null ? "" : String(proposal.source_message_id);
      const anchor = source && messageIds.has(source) ? source : anchorForMissingSource(proposal.created_at);
      if (!anchor) continue;
      map.set(anchor, [...(map.get(anchor) || []), proposal]);
    }
    return map;
  }, [proposals, messages]);
  const pendingCardIds = useMemo(() => {
    const cards: { id: string; source: number }[] = [];
    for (const proposal of proposals) {
      const state = cardStates[`proposal:${proposal.id}`];
      if (proposal.status === "pending" && Date.parse(proposal.expires_at) > Date.now() && state?.status !== "abandoned") {
        cards.push({ id: `proposal:${proposal.id}`, source: proposal.source_message_id ?? 0 });
      }
    }
    for (const card of choiceCards.values()) if (card.status === "pending") cards.push({ id: card.cardId, source: card.sourceMessageId });
    return cards.sort((a, b) => a.source - b.source).map(card => card.id);
  }, [proposals, choiceCards, cardStates]);

  const upsertProposal = useCallback((value: RefundProposal) => {
    setProposals(previous => {
      const rows = previous.map(p => p.id === value.id ? { ...p, ...value } : p);
      return rows.some(p => p.id === value.id) ? rows : [...rows, value];
    });
  }, []);
  const appendStep = useCallback((step: StepEvent) => {
    setSteps(previous => previous.some(s => s.id === step.id) ? previous : [...previous, step].sort((a, b) => a.seq - b.seq));
    if (step.step_type === "card_state") {
      const payload = step.payload as CardStatusEvent;
      setCardStates(previous => ({ ...previous, [payload.card_id]: payload }));
      if (payload.card_type === "refund_proposal") {
        const proposalStatus = payload.status === "abandoned" ? "superseded" : payload.status;
        if (proposalStatus === "confirmed" || proposalStatus === "cancelled" || proposalStatus === "expired" || proposalStatus === "superseded" || proposalStatus === "invalidated") {
          const proposalId = payload.card_id.replace(/^proposal:/, "");
          setProposals(previous => previous.map(proposal => proposal.id === proposalId
            ? { ...proposal, status: proposalStatus } : proposal));
        }
      }
    }
  }, []);
  const appendMessage = useCallback((event: { content: string; message_id?: number }) => {
    const id = event.message_id ? String(event.message_id) : `local-${crypto.randomUUID()}`;
    setMessages(previous => previous.some(m => m.id === id) ? previous : [...previous, { id, role: "assistant", content: event.content, at: Date.now() }]);
  }, []);
  const appendUserMessage = useCallback((event: { content: string; message_id?: number }) => {
    const id = event.message_id ? String(event.message_id) : `local-${crypto.randomUUID()}`;
    setMessages(previous => previous.some(m => m.id === id) ? previous : [...previous, { id, role: "user", content: event.content, at: Date.now() }]);
  }, []);
  const loadOrders = useCallback(async () => {
    const [list, escalations] = await Promise.all([fetchOrders(), fetchMyEscalations()]);
    setOrders(list); setTickets(escalations);
  }, []);
  const loadSnapshot = useCallback(async (cid: string) => {
    const [detail, cards] = await Promise.all([fetchMyConversation(cid), fetchRefundProposals(cid)]);
    setMessages(detail.messages.map(m => ({ id: String(m.id), role: m.role as "user" | "assistant", content: m.content, at: Date.parse(m.created_at) })));
    const restored = detail.events.map(e => ({ ...e, kind: "step" as const }));
    const states: Record<string, CardStatusEvent> = {};
    for (const event of restored) {
      if (event.step_type === "card_state") {
        const cardState = event.payload as CardStatusEvent;
        states[cardState.card_id] = cardState;
      }
    }
    setCardStates(states);
    setTurnMessageId([...detail.messages].reverse().find(message => message.role === "user")?.id ?? null);
    setSteps(restored); setProposals(cards);
  }, []);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        await loadOrders();
        const cid = sessionStorage.getItem(storageKey);
        if (cid && active) {
          try { await loadSnapshot(cid); if (active) { cidRef.current = cid; setConversationId(cid); } }
          catch (err) {
            if (err instanceof ApiError && err.status === 404) sessionStorage.removeItem(storageKey);
            else throw err;
          }
        }
      } catch (err) { if (active) toast(friendlyError(err), "error"); }
      finally { if (active) setLoading(false); }
    };
    void load();
    return () => { active = false; controller.current?.abort(); };
  }, [storageKey, loadOrders, loadSnapshot, toast]);

  const handleEvent = useCallback((event: AgentEvent) => {
    if (event.kind === "step") {
      if (event.message_id != null && event.step_type !== "confirmation") setTurnMessageId(event.message_id);
      appendStep(event);
      if (event.step_type === "tool_result" && event.payload.tool === "offer_order_choices") {
        // The persisted tool-result event is enough to reconstruct this card; its
        // explicit card_state event controls whether it remains actionable.
      }
    } else if (event.kind === "message" && event.role === "assistant") appendMessage(event);
    else if (event.kind === "refund_proposal") upsertProposal(event.proposal);
    else if (event.kind === "escalation") void loadOrders().catch(() => {});
  }, [appendStep, appendMessage, upsertProposal, loadOrders]);
  useEffect(() => {
    if (!conversationId) return;
    return subscribeToMyConversation(conversationId, event => {
      if (cidRef.current === conversationId && !streamingRef.current) handleEvent(event);
    });
  }, [conversationId, handleEvent]);
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); }, [messages, proposals, streaming]);

  function sendFollowingMessage(text: string, orderId?: string | null) {
    void send(text, orderId, pendingCardIds);
  }

  async function submitText(text: string) {
    const normalized = text.trim().toLocaleLowerCase().match(/^(?:选择|选|第)?\s*([a-z]|\d{1,2})(?:号|个)?$/)?.[1];
    // Only the newest pending card can answer a contextual short reply. Never
    // search older cards, because a letter such as "c" has no global meaning.
    const pendingChoices = [...choiceCards.values()].filter(card => card.status === "pending");
    const latestChoice = pendingChoices[pendingChoices.length - 1];
    const index = normalized ? latestChoice?.orders.findIndex((order, i) => choiceLabel(order, i) === normalized) ?? -1 : -1;
    if (latestChoice && index >= 0) {
      // Use exactly the same selected-order clarification as a card click. This
      // preserves the prior intent and avoids resolving the same card twice.
      void chooseOrder(latestChoice.orders[index], latestChoice);
      return;
    }
    sendFollowingMessage(text);
  }

  async function send(text: string, orderIdOverride?: string | null, abandonIds: string[] = [], bypassBusy = false) {
    text = text.trim();
    if (!text || text.length > 2000 || streamingRef.current || (!bypassBusy && busy) || !getToken("customer")) return;
    setTurnMessageId(null);
    const explicitOverride = orderIdOverride !== undefined ? orderIdOverride : choiceOrderOverride;
    // Only send implicit order context after a customer explicitly selected an
    // order in the order strip; the initial fallback card isn't a user choice.
    const orderId = explicitOverride !== undefined
      ? explicitOverride || undefined
      : hasExplicitOrderSelection ? currentOrder?.id : undefined;
    setChoiceOrderOverride(undefined);
    streamingRef.current = true; setStreaming(true); setStartedAt(Date.now()); setEndedAt(null);
    optimisticUserId.current = `local-${crypto.randomUUID()}`;
    setMessages(previous => [...previous, { id: optimisticUserId.current!, role: "user", content: text, at: Date.now() }]);
    setInput("");
    const abort = new AbortController(); controller.current = abort;
    try {
      await streamChat(text, cidRef.current, event => {
        if (abort.signal.aborted) return;
        if (event.kind === "conversation") {
          cidRef.current = event.conversation_id; setConversationId(event.conversation_id);
          sessionStorage.setItem(storageKey, event.conversation_id);
        } else if (event.kind === "error") appendMessage({ content: event.message });
        else if (event.kind === "message" && event.role === "user") {
          const serverId = String(event.message_id ?? "");
          const localId = optimisticUserId.current;
          setMessages(previous => previous.map(message => message.id === localId ? { ...message, id: serverId || localId } : message));
          optimisticUserId.current = null;
        } else handleEvent(event);
      }, abort.signal, orderId, abandonIds);
    } catch (err) {
      if ((err as Error).name !== "AbortError") appendMessage({ content: friendlyError(err) });
    } finally {
      streamingRef.current = false; setStreaming(false); setEndedAt(Date.now()); controller.current = null;
      if (cidRef.current && !abort.signal.aborted) {
        try { setProposals(await fetchRefundProposals(cidRef.current)); await loadOrders(); }
        catch (err) { toast(friendlyError(err), "error"); }
      }
    }
  }

  async function decide(proposal: RefundProposal, decision: "confirm" | "cancel") {
    if (busy || streamingRef.current) return;
    setBusy(proposal.id);
    try {
      const receipt = await decideRefundProposal(proposal.id, decision);
      upsertProposal(receipt.proposal);
      if (receipt.message) appendMessage(receipt.message);
      if (receipt.step) appendStep(receipt.step);
      if (receipt.card_state) appendStep(receipt.card_state);
    } catch (err) { toast(friendlyError(err), "error"); }
    finally {
      try { if (cidRef.current) await loadSnapshot(cidRef.current); await loadOrders(); }
      catch (err) { toast(friendlyError(err), "error"); }
      setBusy(null);
    }
  }

  async function chooseOrder(order: OrderChoice, card: ChoiceCardView) {
    if (streamingRef.current || busy || !conversationId) return;
    setBusy(card.cardId);
    try {
      const state = await decideOrderChoiceCard(conversationId, card.cardId, "selected", order.order_id);
      appendStep(state);
      if (state.action_message) appendUserMessage(state.action_message);
      setChoiceOrderOverride(order.order_id);
      setBusy(null);
      await send(`我指的是订单 ${order.order_id}（${order.product_name}）。请结合我刚才的问题继续帮我处理。`, order.order_id, [], true);
    } catch (err) { toast(friendlyError(err), "error"); }
    finally { setBusy(value => value === card.cardId ? null : value); }
  }

  async function continueDescribing(card: ChoiceCardView) {
    if (streamingRef.current || busy || !conversationId) return;
    setBusy(card.cardId);
    try {
      const state = await decideOrderChoiceCard(conversationId, card.cardId, "skipped");
      appendStep(state);
      if (state.action_message) appendUserMessage(state.action_message);
      setChoiceOrderOverride(null);
    } catch (err) { toast(friendlyError(err), "error"); }
    finally { setBusy(null); composerRef.current?.focus(); }
  }

  function selectOrder(order: Order) {
    if (streamingRef.current || busy || order.id === selected) return;
    // Order selection only changes the context for future turns. Existing
    // proposal cards remain visible at their original turn and stay actionable.
    setSelected(order.id); setHasExplicitOrderSelection(true); sessionStorage.setItem(orderKey, order.id);
    setChoiceOrderOverride(order.id);
  }

  function toggleOrderLayout() {
    const next = orderLayout === "top" ? "left" : "top";
    setOrderLayout(next);
    sessionStorage.setItem(layoutKey, next);
  }

  const status = streaming ? "Agent 正在查询与校验" : proposals.some(p => p.status === "pending") ? "等待你核对确认卡片" : "可以咨询，也可以发起退款申请";
  const orderPanel = (className = "order-top-panel") => <div className={`order-panel ${className}`}>
    <div className="order-panel-title"><Package size={15} /><strong>订单信息</strong></div>
    <div className="refund-scenarios" aria-label="选择演示订单">
      {orders.map((order, index) => {
        const scenario = SCENARIOS[order.scenario || ""];
        return <button key={order.id} disabled={streaming || Boolean(busy)} data-spotlight data-tilt
          className={clsx("motion-card scenario-order", currentOrder?.id === order.id && "is-selected")}
          aria-pressed={currentOrder?.id === order.id} onClick={() => void selectOrder(order)}>
          <span className="flex items-center justify-between gap-2"><strong>{scenario?.label || `订单 ${index + 1}`}</strong><span className="text-brand">{money(order.amount)}</span></span>
          <span className="mt-1 block text-xs text-muted">{order.product_name}</span>
          <small>{order.refunded ? "已退款" : scenario?.detail || "查看订单与退款条件"}</small>
        </button>;
      })}
    </div>
    {currentOrder && <div className="selected-order-info"><Package size={14} /><span>当前订单 <b className="font-mono">{currentOrder.id}</b></span><span>{currentOrder.refunded ? "已退款" : currentOrder.status === "delivered" ? "已签收" : "尚未签收"}</span></div>}
    <PolicySummary />
  </div>;
  const intro = () => <div className={clsx("refund-intro", orderLayout === "left" && "rail-intro")}>
    <div><span className="workspace-eyebrow">一位客户 · 一次完整的退款任务</span><h1>先查清楚，再由你确认。</h1><p>Agent 查询订单、核对规则并准备方案。只有点击确认卡片，才会执行退款或提交人工审核。</p></div>
    <div className="intro-actions">
      <Button size="sm" onClick={toggleOrderLayout} aria-label={orderLayout === "top" ? "切换为左侧订单布局" : "切换为顶部订单布局"} title="切换订单信息布局">
        {orderLayout === "top" ? <LayoutPanelLeft size={14} /> : <Columns2 size={14} />}{orderLayout === "top" ? "订单移至左侧" : "订单移至顶部"}
      </Button>
      {session && <Button size="sm" disabled={streaming || Boolean(busy)} onClick={() => void restart()} title="创建全新的示例订单，已有记录保留"><RefreshCw size={13} />重新体验</Button>}
    </div>
  </div>;
  const chatStatus = () => <div className="refund-chat-status"><span className={streaming ? "signal is-working" : "signal"} />{status}<span className="ml-auto text-subtle">客户身份保持不变</span></div>;
  return <div className={clsx("refund-workspace", orderLayout === "left" && "orders-left")}>
    {orderLayout === "left" && <aside className="order-rail">{intro()}{orderPanel("order-rail-panel")}{chatStatus()}</aside>}
    <section className="refund-conversation">
      {orderLayout === "top" && <>{intro()}{orderPanel("order-top-panel")}{chatStatus()}</>}
      {orderLayout === "left" && <div className="order-top-mobile">{intro()}{orderPanel("order-top-panel")}{chatStatus()}</div>}
      <div ref={scrollRef} className="refund-messages" aria-label="客服对话">
        {loading && <div className="flex items-center gap-2 text-sm text-muted"><Spinner />正在恢复订单与对话…</div>}
        {!loading && messages.length === 0 && <div className="refund-empty">
          <Logo size={36} /><h2>先问问，或直接提出申请。</h2><p>咨询只会查询；退款申请也会停在确认步骤，等待你点击按钮。</p>
          <div className="mt-5 flex justify-center gap-3">
            <Button disabled={!currentOrder || streaming} onClick={() => void send(`订单 ${currentOrder.id} 能退款吗？我先咨询一下条件。`, currentOrder.id)}>先咨询能否退款</Button>
            <Button variant="primary" disabled={!currentOrder || streaming} onClick={() => void send(`我要为订单 ${currentOrder.id} 申请退款，请核对订单并准备待确认申请。`, currentOrder.id)}>发起退款申请</Button>
          </div>
        </div>}
        {messages.map(message => {
          const choices = message.role === "user" ? (cardsAfterMessage.get(message.id) || []) : [];
          const anchoredProposals = message.role === "user" ? (proposalAfterMessage.get(message.id) || []) : [];
          return <Fragment key={message.id}>
            <div className={`refund-message ${message.role}`}>
              <span className="shrink-0">{message.role === "user" ? <UserRound size={22} /> : <Logo size={26} />}</span>
              <div className="message-body">{message.role === "assistant" ? <Markdown>{message.content}</Markdown> : message.content}</div>
            </div>
            {message.role === "user" && choices.map(card => (
              <OrderChoiceCard key={card.cardId} orders={card.orders} status={card.status} selectedOrderId={card.selectedOrderId}
                disabled={Boolean(busy) || streaming} onChoose={order => void chooseOrder(order, card)} onContinue={() => void continueDescribing(card)} />
            ))}
            {message.role === "user" && anchoredProposals.map(proposal => (
              <RefundConfirmation key={proposal.id} proposal={proposal} disabled={streaming} busy={busy} onDecision={(p, decision) => void decide(p, decision)} />
            ))}
          </Fragment>;
        })}
        {streaming && <div className="flex items-center gap-2 text-sm text-muted"><Spinner />正在查询订单与核验退款条件…</div>}
        {ticket && <article className="review-progress" aria-label="人工审核进度">
          <strong className="flex items-center gap-2 text-sm">{ticket.status === "pending" ? <Clock3 size={16} /> : <CheckCircle2 size={16} />}人工审核{ticket.status === "pending" ? "进行中" : ticket.status === "approved" ? "已批准" : "已拒绝"}</strong>
          <p className="mt-2 text-xs text-muted">订单 {ticket.order_id} · {money(ticket.amount)} · 工单 #{ticket.id}</p>
          <p className="mt-2 text-xs leading-6 text-muted">{ticket.status === "pending" ? "申请已提交，尚未退款。人工处理后，结果会在这里更新。" : ticket.resolution_note || "人工处理结果已记录。"}</p>
          <Button size="sm" className="mt-3" onClick={() => void loadOrders().catch(err => toast(friendlyError(err), "error"))}>刷新处理进度</Button>
        </article>}
      </div>
      <form className="refund-composer" onSubmit={event => { event.preventDefault(); void submitText(input); }}>
        <div><label htmlFor="composer" className="sr-only">消息内容</label><textarea ref={composerRef} id="composer" rows={2} value={input} maxLength={2000}
          disabled={!getToken("customer") || loading} onChange={event => {
            const value = event.target.value;
            setInput(value);
            if (typeof choiceOrderOverride === "string" && !value.includes(choiceOrderOverride)) setChoiceOrderOverride(undefined);
          }}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submitText(input); } }}
          placeholder="例如：这笔订单能退吗？或：我想申请退款，商品不太合适。" />
          <button type="submit" aria-label="发送消息" disabled={streaming || Boolean(busy) || loading || !input.trim()}><ArrowUp size={20} /></button></div>
        <p>回车发送 · Shift + 回车换行 <span>咨询不会退款，确认按钮才授权执行</span></p>
      </form>
    </section>
    <aside className="refund-trace"><AgentPanel steps={steps} proposals={proposals} streaming={streaming} startedAt={startedAt} endedAt={endedAt} turnMessageId={turnMessageId} /></aside>
  </div>;
}
