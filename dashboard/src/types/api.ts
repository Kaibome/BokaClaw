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

export type UsageCoverage =
  | "none"
  | "complete"
  | "partial"
  | "unknown"
  | "aggregate"
  | "unavailable"
  | "pending";

export interface UsageSource {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cached_tokens?: number;
  cache_miss_tokens?: number;
  calls: number;
  unknown_calls: number;
}

export interface RunUsage {
  coverage: UsageCoverage;
  transport?: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cached_tokens?: number | null;
  cache_miss_tokens?: number | null;
  cache_hit_rate?: number | null;
  model_calls: number;
  unknown_calls: number;
  sources: Record<string, UsageSource>;
  unmetered_sources?: string[];
  warnings?: string[];
}

export interface ContextUsage {
  used_tokens: number;
  window_tokens: number | null;
  budget_tokens: number | null;
}

/** One `context.compressed` event: the model view was shrunk for this turn. */
export interface ContextCompression {
  before_tokens?: number;
  after_tokens?: number;
  dropped_messages?: number;
  summarized?: boolean;
  fallback?: boolean;
  model_calls?: number;
  usage_unknown_calls?: number;
}

export interface CompressionInfo {
  iteration: number;
  stats: ContextCompression;
  /** How many times this reply compressed its context. */
  count: number;
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
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  model_calls?: number;
  unknown_usage_calls?: number;
  usage_coverage?: UsageCoverage;
  usage?: RunUsage | null;
  context?: ContextUsage | null;
}

export type SpanType = "agent" | "llm" | "tool" | "memory" | "compression";

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
  total_tokens?: number | null;
  cached_tokens?: number | null;
  cache_miss_tokens?: number | null;
  cache_hit_rate?: number | null;
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

export type SubagentStatus =
  | "completed"
  | "partial"
  | "failed"
  | "timeout"
  | "cancelled";

export interface SubagentFact {
  claim: string;
  evidence?: string;
  confidence?: "high" | "medium" | "low";
}

/**
 * Structured envelope returned by the SDK's subagent tool. The parent model
 * receives this as the tool result; the chat renders it as a report instead of
 * raw JSON.
 */
export interface SubagentResult {
  subagent_id: string;
  subagent_type: string;
  status: SubagentStatus;
  report?: string | null;
  data?: Record<string, unknown> | null;
  facts?: SubagentFact[];
  inferences?: string[];
  unknowns?: string[];
  sources?: string[];
  usage?: Record<string, number>;
  turns?: number;
  isolated_chars?: number;
  stop_reason?: string | null;
  duration_ms?: number;
  resumable?: boolean;
  truncated?: boolean;
  error?: string | null;
}

export type UserQuestionStatus =
  | "pending"
  | "submitting"
  | "answered"
  | "expired";

/**
 * One `ask_user` question. The server persists `user.question` / `user.answer`
 * events; the dashboard reconstructs this state from the SSE replay.
 */
export interface UserQuestion {
  question_id: string;
  question: string;
  options: string[];
  allow_free_text: boolean;
  expires_at?: string | null;
  answer?: string | null;
  answered_at?: string | null;
  status?: UserQuestionStatus;
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

export type EvalStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface EvalMetricResult {
  name: string;
  score: number | null;
  threshold: number | null;
  success: boolean | null;
  reason: string | null;
  evaluation_model: string | null;
  evaluation_cost: number | null;
  skipped: boolean;
  error: string | null;
}

export interface EvalRunItem {
  id: string;
  run_id: string;
  run_status: string;
  eval_case_id: string;
  status: EvalStatus;
  use_llm_judge: boolean;
  overall_success: boolean | null;
  judge_model: string | null;
  token_cost: number | null;
  duration_ms: number | null;
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested_at: string | null;
}

export interface EvalRunDetail extends EvalRunItem {
  golden_hash: string;
  attempt: number;
  warnings: string[];
  metrics: EvalMetricResult[];
}

export interface EvalConfig {
  enabled: boolean;
  judge_enabled: boolean;
  judge_provider: string;
  judge_model: string;
  golden_count: number;
  max_attempts: number;
  lease_seconds: number;
}

export interface EvalGolden {
  eval_case_id: string;
  input_preview: string;
  expected_tools: string[];
  updated_at: string;
}

export interface EvalToolCall {
  name: string;
  input_parameters?: Record<string, unknown> | null;
  output?: unknown;
  reasoning?: string | null;
  description?: string | null;
  type?: string | null;
}

export interface EvalGoldenUpsert {
  input: string;
  expected_output: string | null;
  expected_tools: EvalToolCall[];
  context: string[];
  notes: string | null;
}

export interface EvalGoldenDetail extends EvalGoldenUpsert {
  eval_case_id: string;
  created_at: string;
  updated_at: string;
}
