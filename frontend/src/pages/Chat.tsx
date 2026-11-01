import clsx from "clsx";
import { ArrowUp, CheckCircle2, Clock3, Package, RefreshCw, UserRound } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, decideRefundProposal, fetchMyConversation, fetchMyEscalations, fetchOrders, fetchRefundProposals, friendlyError, streamChat, subscribeToMyConversation } from "../api";
import { getToken } from "../auth";
import AgentPanel from "../components/AgentPanel";
import Logo from "../components/Logo";
import Markdown from "../components/Markdown";
import PolicySummary from "../components/PolicySummary";
import RefundConfirmation from "../components/RefundConfirmation";
import { useToast } from "../components/Toaster";
import { Button, Spinner } from "../components/ui";
import { money } from "../lib/outcome";
import { useWorkspaceSession } from "../lib/workspaceSession";
import type { AgentEvent, ChatMessage, Escalation, Order, RefundProposal, StepEvent } from "../types";

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
  const [conversationId, setConversationId] = useState<string | null>(null);
  const cidRef = useRef<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [steps, setSteps] = useState<StepEvent[]>([]);
  const [proposals, setProposals] = useState<RefundProposal[]>([]);
  const [tickets, setTickets] = useState<Escalation[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const streamingRef = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [endedAt, setEndedAt] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const currentOrder = orders.find(order => order.id === selected) || orders[0];
  const ticket = tickets.find(t => t.order_id === currentOrder?.id);

  const upsertProposal = useCallback((value: RefundProposal) => {
    setProposals(previous => {
      const rows = previous.map(p => p.id === value.id ? value :
        value.status === "pending" && p.status === "pending" ? { ...p, status: "superseded" as const } : p);
      return rows.some(p => p.id === value.id) ? rows : [...rows, value];
    });
  }, []);
  const appendStep = useCallback((step: StepEvent) => {
    setSteps(previous => previous.some(s => s.id === step.id) ? previous : [...previous, step].sort((a, b) => a.seq - b.seq));
  }, []);
  const appendMessage = useCallback((event: { content: string; message_id?: number }) => {
    const id = event.message_id ? String(event.message_id) : `local-${crypto.randomUUID()}`;
    setMessages(previous => previous.some(m => m.id === id) ? previous : [...previous, { id, role: "assistant", content: event.content, at: Date.now() }]);
  }, []);
  const loadOrders = useCallback(async () => {
    const [list, escalations] = await Promise.all([fetchOrders(), fetchMyEscalations()]);
    setOrders(list); setTickets(escalations);
  }, []);
  const loadSnapshot = useCallback(async (cid: string) => {
    const [detail, cards] = await Promise.all([fetchMyConversation(cid), fetchRefundProposals(cid)]);
    setMessages(detail.messages.map(m => ({ id: String(m.id), role: m.role as "user" | "assistant", content: m.content, at: Date.parse(m.created_at) })));
    const restored = detail.events.map(e => ({ ...e, kind: "step" as const }));
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
    if (event.kind === "step") appendStep(event);
    else if (event.kind === "message" && event.role === "assistant") appendMessage(event);
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

  async function send(text: string) {
    text = text.trim();
    if (!text || text.length > 2000 || streamingRef.current || busy || !getToken("customer")) return;
    streamingRef.current = true; setStreaming(true); setStartedAt(Date.now()); setEndedAt(null);
    setMessages(previous => [...previous, { id: `local-${crypto.randomUUID()}`, role: "user", content: text, at: Date.now() }]);
    setInput("");
    const abort = new AbortController(); controller.current = abort;
    try {
      await streamChat(text, cidRef.current, event => {
        if (abort.signal.aborted) return;
        if (event.kind === "conversation") {
          cidRef.current = event.conversation_id; setConversationId(event.conversation_id);
          sessionStorage.setItem(storageKey, event.conversation_id);
        } else if (event.kind === "error") appendMessage({ content: event.message });
        else handleEvent(event);
      }, abort.signal, currentOrder?.id);
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
    } catch (err) { toast(friendlyError(err), "error"); }
    finally {
      try { if (cidRef.current) await loadSnapshot(cidRef.current); await loadOrders(); }
      catch (err) { toast(friendlyError(err), "error"); }
      setBusy(null);
    }
  }

  async function selectOrder(order: Order) {
    if (streamingRef.current || busy || order.id === currentOrder?.id) return;
    setBusy("selection");
    try {
      for (const proposal of proposals.filter(p => p.status === "pending")) {
        try { const result = await decideRefundProposal(proposal.id, "cancel"); upsertProposal(result.proposal); }
        catch (err) { if (!(err instanceof ApiError && err.status === 409)) throw err; }
      }
      cidRef.current = null; setConversationId(null); sessionStorage.removeItem(storageKey);
      setMessages([]); setSteps([]); setProposals([]); setInput("");
      setStartedAt(null); setEndedAt(null);
      setSelected(order.id); sessionStorage.setItem(orderKey, order.id);
    } catch (err) { toast(friendlyError(err), "error"); }
    finally { setBusy(null); }
  }

  const status = streaming ? "Agent 正在查询与校验" : proposals.some(p => p.status === "pending") ? "等待你核对确认卡片" : "可以咨询，也可以发起退款申请";
  return <div className="refund-workspace">
    <section className="refund-conversation">
      <div className="refund-intro">
        <div><span className="workspace-eyebrow">一位客户 · 一次完整的退款任务</span><h1>先查清楚，再由你确认。</h1><p>Agent 查询订单、核对规则并准备方案。只有点击确认卡片，才会执行退款或提交人工审核。</p></div>
        {session && <Button size="sm" disabled={streaming || Boolean(busy)} onClick={() => void restart()} title="创建全新的示例订单，已有记录保留"><RefreshCw size={13} />重新体验</Button>}
      </div>
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
      {currentOrder && <div className="selected-order-info"><Package size={14} /><span>当前订单 <b className="font-mono">{currentOrder.id}</b></span><span>{currentOrder.refunded ? "已退款" : currentOrder.status === "delivered" ? "已签收" : "尚未签收"}</span><span className="ml-auto">切换订单会取消待确认申请，并开始新任务</span></div>}
      <PolicySummary />
      <div className="refund-chat-status"><span className={streaming ? "signal is-working" : "signal"} />{status}<span className="ml-auto text-subtle">客户身份保持不变</span></div>
      <div ref={scrollRef} className="refund-messages" aria-label="客服对话">
        {loading && <div className="flex items-center gap-2 text-sm text-muted"><Spinner />正在恢复订单与对话…</div>}
        {!loading && messages.length === 0 && <div className="refund-empty">
          <Logo size={36} /><h2>先问问，或直接提出申请。</h2><p>咨询只会查询；退款申请也会停在确认步骤，等待你点击按钮。</p>
          <div className="mt-5 flex justify-center gap-3">
            <Button disabled={!currentOrder || streaming} onClick={() => void send(`订单 ${currentOrder.id} 能退款吗？我先咨询一下条件。`)}>先咨询能否退款</Button>
            <Button variant="primary" disabled={!currentOrder || streaming} onClick={() => void send(`我要为订单 ${currentOrder.id} 申请退款，请核对订单并准备待确认申请。`)}>发起退款申请</Button>
          </div>
        </div>}
        {messages.map(message => <div key={message.id} className={`refund-message ${message.role}`}>
          <span className="shrink-0">{message.role === "user" ? <UserRound size={22} /> : <Logo size={26} />}</span>
          <div className="message-body">{message.role === "assistant" ? <Markdown>{message.content}</Markdown> : message.content}</div>
        </div>)}
        {streaming && <div className="flex items-center gap-2 text-sm text-muted"><Spinner />正在查询订单与核验退款条件…</div>}
        {!streaming && proposals.map(proposal => <RefundConfirmation key={proposal.id} proposal={proposal} disabled={streaming} busy={busy} onDecision={(p, decision) => void decide(p, decision)} />)}
        {ticket && <article className="review-progress" aria-label="人工审核进度">
          <strong className="flex items-center gap-2 text-sm">{ticket.status === "pending" ? <Clock3 size={16} /> : <CheckCircle2 size={16} />}人工审核{ticket.status === "pending" ? "进行中" : ticket.status === "approved" ? "已批准" : "已拒绝"}</strong>
          <p className="mt-2 text-xs text-muted">订单 {ticket.order_id} · {money(ticket.amount)} · 工单 #{ticket.id}</p>
          <p className="mt-2 text-xs leading-6 text-muted">{ticket.status === "pending" ? "申请已提交，尚未退款。人工处理后，结果会在这里更新。" : ticket.resolution_note || "人工处理结果已记录。"}</p>
          <Button size="sm" className="mt-3" onClick={() => void loadOrders().catch(err => toast(friendlyError(err), "error"))}>刷新处理进度</Button>
        </article>}
      </div>
      <form className="refund-composer" onSubmit={event => { event.preventDefault(); void send(input); }}>
        <div><label htmlFor="composer" className="sr-only">消息内容</label><textarea id="composer" rows={2} value={input} maxLength={2000}
          disabled={!getToken("customer") || loading} onChange={event => setInput(event.target.value)}
          onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(input); } }}
          placeholder="例如：这笔订单能退吗？或：我想申请退款，商品不太合适。" />
          <button type="submit" aria-label="发送消息" disabled={streaming || Boolean(busy) || loading || !input.trim()}><ArrowUp size={20} /></button></div>
        <p>回车发送 · Shift + 回车换行 <span>咨询不会退款，确认按钮才授权执行</span></p>
      </form>
    </section>
    <aside className="refund-trace"><AgentPanel steps={steps} proposals={proposals} streaming={streaming} startedAt={startedAt} endedAt={endedAt} /></aside>
  </div>;
}
