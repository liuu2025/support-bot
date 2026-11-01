export interface StepEvent {
  kind: "step";
  id: number;
  seq: number;
  step_type:
    | "model"
    | "tool_call"
    | "tool_result"
    | "policy_eval"
    | "decision"
    | "proposal"
    | "confirmation"
    | "injection_flag"
    | "usage"
    | "budget_exhausted"
    | "output_correction"
    | "notice"
    | "trace"
    | "error";
  node: string;
  payload: Record<string, any>;
  created_at: string;
}

export interface MessageEvent {
  kind: "message";
  role: "user" | "assistant";
  content: string;
  message_id?: number;
}

export interface ConversationEvent {
  kind: "conversation";
  conversation_id: string;
}

export interface DoneEvent {
  kind: "done";
}

export interface ErrorEvent {
  kind: "error";
  message: string;
}

/** Pushed to the customer's conversation when a human finishes a review. */
export interface EscalationEvent {
  kind: "escalation";
  status: "approved" | "denied";
  order_id: string;
  ticket_id: number;
  note: string;
}

export type AgentEvent =
  | StepEvent
  | MessageEvent
  | ConversationEvent
  | DoneEvent
  | ErrorEvent
  | EscalationEvent
  | { kind: "refund_proposal"; proposal: RefundProposal };

export interface RefundProposal {
  id: string;
  conversation_id: string;
  order_id: string;
  product_name: string;
  amount: number;
  action: "refund" | "review";
  status: "pending" | "confirmed" | "cancelled" | "expired" | "superseded" | "invalidated";
  reasons: string[];
  created_at: string;
  expires_at: string;
  refund_id: number | null;
}

export interface ProposalReceipt {
  proposal: RefundProposal;
  replayed: boolean;
  message?: MessageEvent;
  step?: StepEvent;
}

export interface LocalWorkspaceSession {
  customer: { id: string; name: string };
  token: string;
  orders: Order[];
  expires_in: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  at: number;
  /** Outcome of the turn this assistant message closed (see outcomeOf). */
  outcome?: Outcome | null;
  error?: boolean;
}

export type Outcome =
  "eligible" | "review_required" | "awaiting_confirmation" |
  "approved" | "denied" | "escalated" | "cancelled" | "not_executed" | "blocked" | "flagged";

export interface ConversationSummary {
  id: string;
  customer_id: string | null;
  customer_name: string | null;
  created_at: string;
  message_count: number;
  last_message: string | null;
}

export interface ConversationDetail {
  id: string;
  customer_id: string | null;
  customer_name: string | null;
  messages: { id: number; role: string; content: string; created_at: string }[];
  events: StepEvent[];
}

export type Scenario =
  | "refundable"
  | "final_sale"
  | "high_value"
  | "out_of_window"
  | "already_refunded";

export interface Order {
  id: string;
  product_name: string;
  amount: number;
  status: string;
  order_date: string | null;
  delivered_date: string | null;
  is_final_sale: boolean;
  refunded: boolean;
  scenario: Scenario | null;
}

export interface DemoSession {
  customer: { id: string; name: string; email: string };
  orders: Order[];
  token: string;
  admin_token: string;
  expires_in: number;
  data_ttl_hours: number;
}

export interface Meta {
  local_workspace?: boolean;
  refund_policy?: { return_window_days: number; escalation_threshold: number };
  auth_mode: "dev" | "jwt" | "demo";
  /** The protocol the agent speaks; "unconfigured" until a model is saved. */
  model: "openai-compatible" | "unconfigured" | string;
  /** The model id answering turns; null until an admin configures one. */
  model_name?: string | null;
  demo: {
    session_ttl_s: number;
    data_ttl_hours: number;
    /** How long chats are kept (archived) after the sandbox resets. */
    transcript_retention_days?: number;
  } | null;
}

export interface AdminStats {
  conversations: number;
  messages: number;
  decisions: Record<string, number>;
  injection_flags: number;
  tokens_used: number;
  escalations_pending: number;
  escalations_resolved: number;
  avg_resolution_seconds: number | null;
}

/** A refund the agent handed to a human, and what the human decided. */
export type EscalationStatus = "pending" | "approved" | "denied";

export interface Escalation {
  id: number;
  order_id: string;
  product_name: string;
  amount: number;
  status: EscalationStatus;
  decision: string;
  reason: string;
  customer_id: string | null;
  customer_name: string | null;
  conversation_id: string | null;
  escalated_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_note: string;
  waiting_seconds: number | null;
  handled_seconds: number | null;
}

export interface EscalationDetail extends Escalation {
  transcript: { role: string; content: string; created_at: string }[];
}

export interface EscalationStats {
  pending: number;
  by_status: Record<string, number>;
  resolved: number;
  avg_resolution_seconds: number | null;
}

// ---------------------------------------------------------------------------
// Model settings (OpenAI-compatible endpoint, edited in the UI)
// ---------------------------------------------------------------------------

/** Where the effective configuration came from: saved row, env, or nothing. */
export type ModelConfigSource = "db" | "env" | "none";

export interface ModelConfig {
  base_url: string;
  model_name: string;
  timeout_s: number;
  /** Bumped on every save; cached clients rebuild when it changes. */
  version: number;
  source: ModelConfigSource;
  configured: boolean;
  /** The key itself is never returned — only whether one is stored. */
  has_api_key: boolean;
}

export interface ModelPreset {
  id: number;
  name: string;
  base_url: string;
  model_name: string;
  timeout_s: number;
  has_api_key: boolean;
  updated_at: string;
}

export interface ModelProbeResult {
  ok: boolean;
  detail: string;
  tool_called: string;
  latency_ms: number;
}
