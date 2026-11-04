import { getToken, setToken, type Role } from "./auth";
import { apiUrl } from "./config";
import type {
  AdminStats,
  AgentEvent,
  ConversationDetail,
  ConversationSummary,
  DemoSession,
  Escalation,
  EscalationDetail,
  EscalationStats,
  EscalationStatus,
  Meta,
  ModelConfig,
  ModelPreset,
  ModelProbeResult,
  MessageEvent,
  Order,
  RefundProposal,
  ProposalReceipt,
  StepEvent,
  LocalWorkspaceSession,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public retryAfter?: number,
  ) {
    super(message);
  }
}

/** A user-facing message for each failure the backend can return. */
export function friendlyError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.status) {
      case 0:
        return err.message;
      case 401:
        return "登录状态已过期，请重新登录。";
      case 403:
        return "你没有访问该内容的权限。";
      case 404:
        return "请求的内容不存在，请刷新后重试。";
      case 409:
        return "当前状态已变化或有操作正在处理，请刷新后重试。";
      case 429:
        return err.message || "发送太频繁了，请稍后再试。";
      case 503:
        return `系统当前负载较高，请在 ${err.retryAfter ?? 5} 秒后重试。`;
    }
    return err.message;
  }
  return (err as Error)?.message ?? "出错了，请稍后再试。";
}

async function request(
  role: Role | null,
  url: string,
  init: RequestInit = {},
): Promise<Response> {
  const token = role ? getToken(role) : null;
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(apiUrl(url), { ...init, headers });
  } catch (err) {
    if ((err as Error)?.name === "AbortError") throw err;
    throw new ApiError(0, "无法连接服务器，请检查网络后重试。");
  }
  if (!res.ok) {
    // A late rejection from an old request must not sign out a newer session.
    if (res.status === 401 && role && token && getToken(role) === token) {
      setToken(role, null);
    }
    let detail = `请求失败（HTTP ${res.status}）`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    const retry = Number(res.headers.get("Retry-After")) || undefined;
    throw new ApiError(res.status, detail, retry);
  }
  return res;
}

/**
 * Read Server-Sent Events from a fetch response body. Used instead of
 * EventSource because EventSource can neither POST nor send an
 * Authorization header.
 */
async function readSSE(
  res: Response,
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const flush = (frame: string) => {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data) onEvent(JSON.parse(data) as AgentEvent);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line (\n\n or \r\n\r\n).
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    for (const frame of frames) flush(frame);
  }
  if (buffer.trim()) flush(buffer);
}

/** Stream a chat turn as the signed-in customer. */
export async function streamChat(
  message: string,
  conversationId: string | null,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
  orderId?: string,
  abandonCardIds: string[] = [],
): Promise<void> {
  const res = await request("customer", "/api/chat", {
    method: "POST",
    body: JSON.stringify({ message, conversation_id: conversationId, order_id: orderId || null, abandon_card_ids: abandonCardIds }),
    signal,
  });
  await readSSE(res, onEvent);
}

export async function fetchConversations(): Promise<ConversationSummary[]> {
  return (await request("admin", "/api/conversations?limit=100")).json();
}

export async function fetchConversation(
  id: string,
): Promise<ConversationDetail> {
  return (
    await request("admin", `/api/conversations/${encodeURIComponent(id)}`)
  ).json();
}

/**
 * Subscribe (as admin) to live reasoning events for a conversation. Reconnects
 * with backoff if the stream drops (e.g. a pod is replaced). Returns a closer.
 */
function subscribeStream(
  role: Role,
  path: string,
  onEvent: (event: AgentEvent) => void,
): () => void {
  const controller = new AbortController();
  let attempt = 0;

  const connect = async () => {
    while (!controller.signal.aborted) {
      try {
        const res = await request(role, path, { signal: controller.signal });
        attempt = 0;
        await readSSE(res, onEvent);
      } catch (err) {
        if (controller.signal.aborted) return;
        if (
          err instanceof ApiError &&
          (err.status === 401 || err.status === 403)
        )
          return;
      }
      attempt += 1;
      await new Promise((r) =>
        setTimeout(r, Math.min(15000, 500 * 2 ** attempt)),
      );
    }
  };
  void connect();
  return () => controller.abort();
}

export function subscribeToConversation(
  id: string,
  onEvent: (event: AgentEvent) => void,
): () => void {
  return subscribeStream(
    "admin",
    `/api/conversations/${encodeURIComponent(id)}/stream`,
    onEvent,
  );
}

/**
 * Subscribe (as the customer) to their own conversation, so a human reviewer's
 * decision arrives without a refresh. Same reconnect behaviour.
 */
export function subscribeToMyConversation(
  id: string,
  onEvent: (event: AgentEvent) => void,
): () => void {
  return subscribeStream(
    "customer",
    `/api/me/conversations/${encodeURIComponent(id)}/stream`,
    onEvent,
  );
}

export async function fetchMeta(): Promise<Meta> {
  return (await request(null, "/api/meta")).json();
}

export async function createDemoSession(
  turnstileToken?: string,
): Promise<DemoSession> {
  const res = await request(null, "/api/demo/session", {
    method: "POST",
    body: JSON.stringify({ turnstile_token: turnstileToken ?? null }),
  });
  return res.json();
}

export async function fetchOrders(): Promise<Order[]> {
  return (await request("customer", "/api/me/orders")).json();
}

export async function createLocalWorkspace(): Promise<LocalWorkspaceSession> {
  return (await request(null, "/api/local/session", { method: "POST" })).json();
}

export async function fetchMyConversation(id: string): Promise<ConversationDetail> {
  return (await request("customer", `/api/me/conversations/${encodeURIComponent(id)}`)).json();
}

export async function fetchRefundProposals(cid: string): Promise<RefundProposal[]> {
  return (await request("customer", `/api/me/refund-proposals?conversation_id=${encodeURIComponent(cid)}`)).json();
}

export type OrderChoiceDecisionReceipt = StepEvent & { action_message?: MessageEvent };

export async function decideOrderChoiceCard(
  conversationId: string, cardId: string, action: "selected" | "skipped", orderId?: string,
): Promise<OrderChoiceDecisionReceipt> {
  return (await request("customer", `/api/me/order-choice-cards/${encodeURIComponent(cardId)}/decision?conversation_id=${encodeURIComponent(conversationId)}`, {
    method: "POST", body: JSON.stringify({ action, order_id: orderId ?? null }),
  })).json();
}

export async function decideRefundProposal(id: string, decision: "confirm" | "cancel"): Promise<ProposalReceipt> {
  return (await request("customer", `/api/me/refund-proposals/${encodeURIComponent(id)}/decision`, {
    method: "POST", body: JSON.stringify({ decision }),
  })).json();
}

export async function fetchStats(): Promise<AdminStats> {
  return (await request("admin", "/api/admin/stats")).json();
}

// ---------------------------------------------------------------------------
// Human-in-the-loop (reviewer workbench)
// ---------------------------------------------------------------------------

export async function fetchEscalations(
  status: EscalationStatus | "all" = "pending",
): Promise<Escalation[]> {
  return (
    await request("admin", `/api/agent/escalations?status=${status}&limit=100`)
  ).json();
}

export async function fetchEscalation(id: number): Promise<EscalationDetail> {
  return (await request("admin", `/api/agent/escalations/${id}`)).json();
}

export async function fetchEscalationStats(): Promise<EscalationStats> {
  return (await request("admin", "/api/agent/escalations/stats")).json();
}

export async function resolveEscalation(
  id: number,
  decision: "approved" | "denied",
  note: string,
): Promise<EscalationDetail> {
  const res = await request("admin", `/api/agent/escalations/${id}/resolve`, {
    method: "POST",
    body: JSON.stringify({ decision, note }),
  });
  return res.json();
}

/** The signed-in customer's own escalation tickets (newest first). */
export async function fetchMyEscalations(): Promise<Escalation[]> {
  return (await request("customer", "/api/me/escalations?status=all")).json();
}

// ---------------------------------------------------------------------------
// Model settings (OpenAI-compatible endpoint). Admin-only, unscoped.
// ---------------------------------------------------------------------------

export async function fetchModelConfig(): Promise<ModelConfig> {
  return (await request("admin", "/api/admin/model-config")).json();
}

export async function saveModelConfig(input: {
  base_url: string;
  model_name: string;
  api_key: string;
  timeout_s: number;
}): Promise<ModelConfig> {
  const res = await request("admin", "/api/admin/model-config", {
    method: "PUT",
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function clearModelConfig(): Promise<void> {
  await request("admin", "/api/admin/model-config", { method: "DELETE" });
}

/** Ask the endpoint which models it serves — this is what fills the dropdown. */
export async function fetchModelOptions(
  base_url: string,
  api_key: string,
): Promise<string[]> {
  const res = await request("admin", "/api/admin/model-config/models", {
    method: "POST",
    body: JSON.stringify({ base_url, api_key }),
  });
  return (await res.json()).models ?? [];
}

/** One real tool-calling round trip against the saved configuration. */
export async function testModelConfig(): Promise<ModelProbeResult> {
  const res = await request("admin", "/api/admin/model-config/test", {
    method: "POST",
  });
  return res.json();
}

export async function fetchModelPresets(): Promise<ModelPreset[]> {
  return (await request("admin", "/api/admin/model-config/presets")).json();
}

export async function saveModelPreset(input: {
  name: string;
  base_url: string;
  model_name: string;
  api_key: string;
  timeout_s: number;
}): Promise<ModelPreset> {
  const res = await request("admin", "/api/admin/model-config/presets", {
    method: "POST",
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function renameModelPreset(
  id: number,
  name: string,
): Promise<ModelPreset> {
  const res = await request("admin", `/api/admin/model-config/presets/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
  return res.json();
}

export async function deleteModelPreset(id: number): Promise<void> {
  await request("admin", `/api/admin/model-config/presets/${id}`, {
    method: "DELETE",
  });
}

export async function applyModelPreset(id: number): Promise<ModelConfig> {
  const res = await request(
    "admin",
    `/api/admin/model-config/presets/${id}/apply`,
    {
      method: "POST",
    },
  );
  return res.json();
}
