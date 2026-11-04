export interface Memory {
  id: string;
  memory: string;
  user_id?: string;
  agent_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ApiKey {
  id: string;
  label: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ApiKeyCreateResponse {
  id: string;
  label: string;
  key: string;
  key_prefix: string;
  created_at: string;
}

export interface ApiRequestLog {
  id: string;
  created_at: string;
  method: string;
  path: string;
  status_code: number;
  latency_ms: number;
  auth_type: string;
}

export type ChatRole = "user" | "assistant" | "system" | "tool";
export type MessageStatus = "completed" | "cancelled" | "failed";

export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export type ConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

export interface ActiveRun {
  id: string;
  status: RunStatus;
  last_seq: number;
  assistant_message_id: string | null;
  cancel_requested_at: string | null;
  created_at: string;
}

export interface AgentRun {
  id: string;
  conversation_id: string;
  status: RunStatus;
  error_code: string | null;
  error_detail: string | null;
  last_seq: number;
  partial_content: string;
  assistant_message_id: string | null;
  cancel_requested_at: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type SpanType = "agent" | "llm" | "tool";

export type SpanStatus =
  | "running"
  | "completed"
  | "failed"
  | "aborted"
  | "interrupted";

export interface AgentRunSpanSummary {
  id: number;
  run_id: string;
  span_id: string;
  parent_span_id: string | null;
  span_type: SpanType;
  name: string;
  status: SpanStatus;
  iteration: number | null;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  error: string | null;
}

export interface AgentRunSpanDetail extends AgentRunSpanSummary {
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  attributes: Record<string, unknown>;
}

export type ToolTraceStatus = "running" | "completed" | "failed" | "aborted";

export interface ToolTraceEntry {
  tool_call_id: string;
  name: string;
  iteration?: number;
  status: ToolTraceStatus;
  arguments?: Record<string, unknown> | null;
  raw_arguments?: string | null;
  arguments_error?: string | null;
  content?: string;
  is_error?: boolean;
  duration_ms?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  truncated?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  agent_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: ChatRole;
  content: string;
  status: MessageStatus;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
  active_run?: ActiveRun | null;
}

export interface AgentInvokeResponse {
  conversation: Conversation;
  user_message: ChatMessage;
  message: ChatMessage;
  run_id?: string;
}

export type EntityType = "user" | "agent" | "run";

export interface Entity {
  id: string;
  type: EntityType;
  total_memories: number;
  created_at: string | null;
  updated_at: string | null;
}
