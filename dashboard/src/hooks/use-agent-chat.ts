"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { ApiStreamError, api, getStream, postStream } from "@/utils/api";
import { AGENT_ENDPOINTS } from "@/utils/api-endpoints";
import { SseParser, type SseMessage } from "@/utils/sse";
import type {
  ActiveRun,
  AgentRun,
  ChatMessage,
  ConnectionState,
  Conversation,
  ConversationDetail,
  RunStatus,
  ToolTraceEntry,
  ToolTraceStatus,
} from "@/types/api";

export interface ChatMessageView extends ChatMessage {
  streaming?: boolean;
  failed?: boolean;
  toolTrace?: ToolTraceEntry[];
  runStatus?: RunStatus;
  reconnecting?: boolean;
  cancelRequested?: boolean;
}

export interface SendResult {
  ok: boolean;
  draft?: string;
}

interface BasePayload {
  run_id?: string;
  conversation_id?: string;
  seq?: number;
}

interface MetaPayload extends BasePayload {
  status?: RunStatus;
  last_seq?: number;
  assistant_message_id?: string | null;
  conversation?: Conversation;
  user_message?: ChatMessage;
  message?: ChatMessage | null;
  cancel_requested_at?: string | null;
}

interface DeltaPayload extends BasePayload {
  message_id: string;
  delta: string;
}

interface CompletedPayload extends BasePayload {
  message: ChatMessage;
}

interface ToolCallPayload extends BasePayload {
  message_id: string;
  tool_call_id: string;
  name: string;
  iteration?: number;
  status?: ToolTraceStatus;
  arguments?: Record<string, unknown> | null;
  raw_arguments?: string | null;
  arguments_error?: string | null;
  started_at?: string | null;
  truncated?: boolean;
}

interface ToolResultPayload extends BasePayload {
  message_id: string;
  tool_call_id: string;
  name: string;
  iteration?: number;
  status?: ToolTraceStatus;
  content?: string;
  is_error?: boolean;
  duration_ms?: number | null;
  finished_at?: string | null;
  truncated?: boolean;
}

interface ErrorPayload extends BasePayload {
  code?: string;
  detail?: string;
  partial?: boolean;
}

interface DonePayload extends BasePayload {
  status?: RunStatus;
  last_seq?: number;
}

const TOOL_TRACE_STATUSES: ToolTraceStatus[] = [
  "running",
  "completed",
  "failed",
  "aborted",
];

const TERMINAL_RUN_STATUSES: RunStatus[] = [
  "completed",
  "failed",
  "cancelled",
  "interrupted",
];

const WATCHDOG_MS = 45_000;
const POLL_MS = 5_000;
const MAX_RECONNECT_FAILURES = 8;
const BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
const CURSOR_VERSION = 1;

const cursorKey = (conversationId: string) =>
  `bokaclaw.agent.run.v${CURSOR_VERSION}:${conversationId}`;

interface RunCursor {
  v: typeof CURSOR_VERSION;
  conversationId: string;
  runId: string;
  assistantMessageId: string;
  lastSeq: number;
  updatedAt: string;
}

function isTerminalStatus(
  status: RunStatus | null | undefined,
): status is RunStatus {
  return status !== null && status !== undefined
    ? TERMINAL_RUN_STATUSES.includes(status)
    : false;
}

function isActiveStatus(status: RunStatus | null | undefined): boolean {
  return status === "queued" || status === "running";
}

function httpStatus(err: unknown): number | undefined {
  if (err instanceof ApiStreamError) return err.status;
  if (typeof err === "object" && err !== null && "response" in err) {
    const response = (err as { response?: { status?: number } }).response;
    return response?.status;
  }
  return undefined;
}

function isToolTraceStatus(value: unknown): value is ToolTraceStatus {
  return (
    typeof value === "string" &&
    TOOL_TRACE_STATUSES.includes(value as ToolTraceStatus)
  );
}

function normalizeToolTrace(raw: unknown): ToolTraceEntry[] | undefined {
  if (!Array.isArray(raw)) return undefined;

  const entries: ToolTraceEntry[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const toolCallId = record.tool_call_id;
    const name = record.name;
    if (typeof toolCallId !== "string" || !toolCallId) continue;
    if (typeof name !== "string" || !name) continue;

    const isError = record.is_error === true;
    entries.push({
      ...record,
      tool_call_id: toolCallId,
      name,
      status: isToolTraceStatus(record.status)
        ? record.status
        : isError
          ? "failed"
          : "completed",
      is_error: isError,
    } as ToolTraceEntry);
  }
  return entries.length ? entries : undefined;
}

function upsertToolTrace(
  entries: ToolTraceEntry[] | undefined,
  incoming: ToolTraceEntry,
): ToolTraceEntry[] {
  const list = entries ?? [];
  const index = list.findIndex(
    (entry) => entry.tool_call_id === incoming.tool_call_id,
  );
  if (index === -1) return [...list, incoming];

  const existing = list[index];
  const merged: ToolTraceEntry = {
    ...existing,
    ...incoming,
    // A result frame omits arguments; keep what tool.call captured.
    arguments: incoming.arguments ?? existing.arguments,
    raw_arguments: incoming.raw_arguments ?? existing.raw_arguments,
    arguments_error: incoming.arguments_error ?? existing.arguments_error,
    started_at: incoming.started_at ?? existing.started_at,
  };
  if (existing.status !== "running" && incoming.status === "running") {
    merged.status = existing.status;
  }

  const next = [...list];
  next[index] = merged;
  return next;
}

function finalizeRunningToolTrace(
  entries: ToolTraceEntry[] | undefined,
): ToolTraceEntry[] | undefined {
  if (!entries?.length) return entries;
  return entries.map((entry) =>
    entry.status === "running"
      ? { ...entry, status: "aborted" as const, is_error: true }
      : entry,
  );
}

const NOT_CONFIGURED_MESSAGE =
  "Agent execution is not configured yet. Set AGENT_MODEL and AGENT_API_KEY (or OPENAI_API_KEY) on the API server, or AGENT_LOOP_MOCK=true for local UI work.";

function upsertConversation(
  conversations: Conversation[],
  next: Conversation,
): Conversation[] {
  return [next, ...conversations.filter((item) => item.id !== next.id)].sort(
    (a, b) => b.updated_at.localeCompare(a.updated_at),
  );
}

function toView(message: ChatMessage): ChatMessageView {
  const toolTrace = normalizeToolTrace(message.metadata?.tool_trace);
  const view: ChatMessageView = { ...message };

  if (message.role === "assistant") {
    if (message.metadata?.run_status === "interrupted") {
      view.runStatus = "interrupted";
    } else if (message.status === "cancelled") {
      view.runStatus = "cancelled";
    } else if (message.status === "failed") {
      view.runStatus = "failed";
    } else {
      view.runStatus = "completed";
    }
  }

  if (toolTrace) view.toolTrace = toolTrace;
  return view;
}

function parse<T>(message: SseMessage): T | null {
  try {
    return JSON.parse(message.data) as T;
  } catch {
    return null;
  }
}

export function useAgentChat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessageView[]>([]);
  const [isLoadingConversations, setIsLoadingConversations] = useState(true);
  const [isLoadingMessages, setIsLoadingMessages] = useState(false);
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeIdRef = useRef<string | null>(null);
  const runIdRef = useRef<string | null>(null);
  const lastSeqRef = useRef(0);
  const connectionRef = useRef<ConnectionState>("idle");
  const abortRef = useRef<AbortController | null>(null);
  const assistantIdRef = useRef<string | null>(null);
  const cursorKeyRef = useRef<string | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const watchdogRef = useRef<number | null>(null);
  const pollTimerRef = useRef<number | null>(null);
  const pollingRef = useRef(false);
  const consecutiveFailuresRef = useRef(0);
  const terminalRef = useRef(false);
  const sendingRef = useRef(false);
  const cancelRequestedRef = useRef(false);
  const runStatusRef = useRef<RunStatus | null>(null);
  const lastEventAtRef = useRef(0);
  const epochRef = useRef(0);
  const staleRunIdsRef = useRef<Set<string>>(new Set());

  const connectRef = useRef<(runId: string, afterSeq: number) => Promise<void>>(
    async () => {},
  );
  const scheduleReconnectRef = useRef<() => void>(() => {});
  const pollRunRef = useRef<() => void>(() => {});
  const loadMessagesRef = useRef<(conversationId: string) => Promise<void>>(
    async () => {},
  );

  const clearRetryTimer = useCallback(() => {
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }, []);

  const clearWatchdog = useCallback(() => {
    if (watchdogRef.current !== null) {
      window.clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  const stopPolling = useCallback(() => {
    pollingRef.current = false;
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // Closing a subscription is local-only: it never cancels the server run.
  const closeSubscription = useCallback(() => {
    epochRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    clearRetryTimer();
    clearWatchdog();
    stopPolling();
  }, [clearRetryTimer, clearWatchdog, stopPolling]);

  const loadConversations = useCallback(async () => {
    const response = await api.get<Conversation[]>(
      AGENT_ENDPOINTS.CONVERSATIONS,
    );
    const list = Array.isArray(response.data) ? response.data : [];
    setConversations(list);
    return list;
  }, []);

  const patchAssistant = useCallback(
    (
      messageId: string | null,
      updater: (message: ChatMessageView) => ChatMessageView,
    ) => {
      if (!messageId) return;
      setMessages((previous) => {
        let found = false;
        const next = previous.map((message) => {
          if (message.id !== messageId) return message;
          found = true;
          return updater(message);
        });
        if (found) return next;

        return [
          ...next,
          updater({
            id: messageId,
            conversation_id: activeIdRef.current ?? "",
            role: "assistant",
            content: "",
            status: "completed",
            metadata: {},
            created_at: new Date().toISOString(),
            streaming: true,
          }),
        ];
      });
    },
    [],
  );

  const ensureAssistantBubble = useCallback(
    (messageId: string | null | undefined, conversationId?: string) => {
      if (!messageId) return;
      const previousId = assistantIdRef.current;
      assistantIdRef.current = messageId;

      setMessages((previous) => {
        if (previous.some((message) => message.id === messageId)) {
          return previous;
        }
        if (
          previousId &&
          previous.some((message) => message.id === previousId)
        ) {
          return previous.map((message) =>
            message.id === previousId ? { ...message, id: messageId } : message,
          );
        }
        const tempIndex = previous.findIndex(
          (message) =>
            message.role === "assistant" &&
            message.id.startsWith("temp-assistant-"),
        );
        if (tempIndex >= 0) {
          const next = [...previous];
          next[tempIndex] = { ...next[tempIndex], id: messageId };
          return next;
        }
        return [
          ...previous,
          {
            id: messageId,
            conversation_id: conversationId ?? activeIdRef.current ?? "",
            role: "assistant",
            content: "",
            status: "completed",
            metadata: {},
            created_at: new Date().toISOString(),
            streaming: true,
          },
        ];
      });
    },
    [],
  );

  const clearCursor = useCallback((conversationId?: string | null) => {
    const id = conversationId ?? activeIdRef.current;
    if (!id) return;
    const key = cursorKey(id);
    try {
      window.sessionStorage.removeItem(key);
    } catch {}
    if (cursorKeyRef.current === key) cursorKeyRef.current = null;
  }, []);

  const persistCursor = useCallback(() => {
    const key = cursorKeyRef.current;
    const runId = runIdRef.current;
    const assistantMessageId = assistantIdRef.current;
    const conversationId = activeIdRef.current;
    if (!key || !runId || !assistantMessageId || !conversationId) return;

    try {
      const cursor: RunCursor = {
        v: CURSOR_VERSION,
        conversationId,
        runId,
        assistantMessageId,
        lastSeq: lastSeqRef.current,
        updatedAt: new Date().toISOString(),
      };
      window.sessionStorage.setItem(key, JSON.stringify(cursor));
    } catch {}
  }, []);

  const acceptSeq = useCallback(
    (event: SseMessage, payload: BasePayload | null) => {
      const seq = Number(event.id ?? payload?.seq ?? 0);
      if (!Number.isFinite(seq) || seq <= 0) return true; // meta/done carry no id
      if (seq <= lastSeqRef.current) return false;
      lastSeqRef.current = seq;
      persistCursor();
      return true;
    },
    [persistCursor],
  );

  const finishRun = useCallback(
    (status: RunStatus) => {
      terminalRef.current = true;
      runStatusRef.current = status;
      setRunStatus(status);
      cancelRequestedRef.current = false;
      setCancelRequested(false);
      sendingRef.current = false;
      setIsSending(false);
      runIdRef.current = null;
      setActiveRunId(null);
      consecutiveFailuresRef.current = 0;
      connectionRef.current = "closed";
      setConnection("closed");
      clearCursor();
      closeSubscription();
      setMessages((previous) =>
        previous.map((message) =>
          message.role === "assistant" &&
          (message.streaming ||
            message.reconnecting ||
            message.id === assistantIdRef.current)
            ? {
                ...message,
                streaming: false,
                reconnecting: false,
                runStatus: status,
                failed: status === "failed",
                status:
                  status === "cancelled"
                    ? "cancelled"
                    : status === "failed"
                      ? "failed"
                      : message.status,
                toolTrace: finalizeRunningToolTrace(message.toolTrace),
              }
            : message,
        ),
      );
      void loadConversations().catch(() => {});
    },
    [clearCursor, closeSubscription, loadConversations],
  );

  const stopLocalRun = useCallback(
    (nextConnection: ConnectionState) => {
      terminalRef.current = true;
      runIdRef.current = null;
      lastSeqRef.current = 0;
      runStatusRef.current = null;
      consecutiveFailuresRef.current = 0;
      setRunStatus(null);
      setActiveRunId(null);
      cancelRequestedRef.current = false;
      setCancelRequested(false);
      sendingRef.current = false;
      setIsSending(false);
      connectionRef.current = nextConnection;
      setConnection(nextConnection);
      closeSubscription();
      setMessages((previous) =>
        previous.map((message) =>
          message.role === "assistant" &&
          (message.streaming || message.reconnecting)
            ? {
                ...message,
                streaming: false,
                reconnecting: false,
                toolTrace: finalizeRunningToolTrace(message.toolTrace),
              }
            : message,
        ),
      );
    },
    [closeSubscription],
  );

  const armWatchdog = useCallback(
    (epoch: number) => {
      clearWatchdog();
      watchdogRef.current = window.setTimeout(() => {
        watchdogRef.current = null;
        if (
          terminalRef.current ||
          epoch !== epochRef.current ||
          !runIdRef.current
        ) {
          return;
        }
        abortRef.current?.abort();
        abortRef.current = null;
        consecutiveFailuresRef.current += 1;
        scheduleReconnectRef.current();
      }, WATCHDOG_MS);
    },
    [clearWatchdog],
  );

  const handleEvent = useCallback(
    (event: SseMessage) => {
      if (event.event === "meta") {
        const payload = parse<MetaPayload>(event);
        if (!payload) return;

        consecutiveFailuresRef.current = 0;
        stopPolling();
        clearRetryTimer();
        connectionRef.current = "open";
        setConnection("open");

        const conversation = payload.conversation;
        if (conversation) {
          activeIdRef.current = conversation.id;
          setActiveId(conversation.id);
          setConversations((previous) =>
            upsertConversation(previous, conversation),
          );
        }

        const conversationId =
          conversation?.id ??
          payload.conversation_id ??
          activeIdRef.current ??
          undefined;

        if (payload.run_id) {
          runIdRef.current = payload.run_id;
          setActiveRunId(payload.run_id);
          if (conversationId) {
            cursorKeyRef.current = cursorKey(conversationId);
          }
        }

        if (payload.status) {
          runStatusRef.current = payload.status;
          setRunStatus(payload.status);
        }

        if (payload.user_message) {
          const serverUser = toView(payload.user_message);
          setMessages((previous) => {
            const index = previous.findIndex(
              (message) =>
                message.id === serverUser.id ||
                (message.role === "user" &&
                  message.id.startsWith("temp-user-")),
            );
            if (index >= 0) {
              const next = [...previous];
              next[index] = serverUser;
              return next;
            }
            const assistantIndex = previous.findIndex(
              (message) =>
                message.role === "assistant" &&
                message.id ===
                  (payload.assistant_message_id ?? assistantIdRef.current),
            );
            if (assistantIndex >= 0) {
              const next = [...previous];
              next.splice(assistantIndex, 0, serverUser);
              return next;
            }
            return [...previous, serverUser];
          });
        }

        if (payload.assistant_message_id) {
          ensureAssistantBubble(payload.assistant_message_id, conversationId);
        }

        if (payload.cancel_requested_at) {
          cancelRequestedRef.current = true;
          setCancelRequested(true);
        }

        const status = payload.status;
        if (status && isTerminalStatus(status) && payload.message) {
          const finalMessage = payload.message;
          patchAssistant(finalMessage.id, (message) => ({
            ...toView(finalMessage),
            streaming: false,
            reconnecting: false,
            runStatus: status,
            failed: status === "failed",
            cancelRequested: false,
            toolTrace:
              normalizeToolTrace(finalMessage.metadata?.tool_trace) ??
              message.toolTrace,
          }));
          setMessages((previous) =>
            previous.map((message) =>
              message.reconnecting
                ? { ...message, reconnecting: false }
                : message,
            ),
          );
          finishRun(status);
          return;
        }

        if (status && isTerminalStatus(status)) {
          setMessages((previous) =>
            previous.map((message) =>
              message.role === "assistant" && message.streaming
                ? {
                    ...message,
                    streaming: false,
                    reconnecting: false,
                    runStatus: status,
                    failed: status === "failed",
                    toolTrace: finalizeRunningToolTrace(message.toolTrace),
                  }
                : message,
            ),
          );
        } else {
          setMessages((previous) =>
            previous.map((message) =>
              message.role === "assistant" &&
              (message.streaming || message.reconnecting)
                ? {
                    ...message,
                    reconnecting: false,
                    streaming: true,
                    runStatus: payload.status ?? message.runStatus,
                  }
                : message,
            ),
          );
        }

        persistCursor();
        return;
      }

      if (event.event === "message.delta") {
        const payload = parse<DeltaPayload>(event);
        if (!payload || !acceptSeq(event, payload)) return;

        ensureAssistantBubble(payload.message_id, payload.conversation_id);
        patchAssistant(payload.message_id, (message) => ({
          ...message,
          content: message.content + payload.delta,
          streaming: true,
          reconnecting: false,
          cancelRequested: cancelRequestedRef.current,
          runStatus:
            message.runStatus && isTerminalStatus(message.runStatus)
              ? message.runStatus
              : "running",
        }));
        if (runStatusRef.current !== "running") {
          runStatusRef.current = "running";
          setRunStatus("running");
        }
        return;
      }

      if (event.event === "tool.call") {
        const payload = parse<ToolCallPayload>(event);
        if (!payload || !acceptSeq(event, payload)) return;

        ensureAssistantBubble(payload.message_id, payload.conversation_id);
        const entry: ToolTraceEntry = {
          tool_call_id: payload.tool_call_id,
          name: payload.name,
          iteration: payload.iteration,
          status: payload.status ?? "running",
          arguments: payload.arguments ?? null,
          raw_arguments: payload.raw_arguments ?? null,
          arguments_error: payload.arguments_error ?? null,
          content: "",
          is_error: false,
          started_at: payload.started_at ?? null,
          truncated: payload.truncated ?? false,
        };
        patchAssistant(payload.message_id, (message) => ({
          ...message,
          streaming: true,
          reconnecting: false,
          cancelRequested: cancelRequestedRef.current,
          toolTrace: upsertToolTrace(message.toolTrace, entry),
        }));
        return;
      }

      if (event.event === "tool.result") {
        const payload = parse<ToolResultPayload>(event);
        if (!payload || !acceptSeq(event, payload)) return;

        ensureAssistantBubble(payload.message_id, payload.conversation_id);
        const isError = payload.is_error === true;
        const entry: ToolTraceEntry = {
          tool_call_id: payload.tool_call_id,
          name: payload.name,
          iteration: payload.iteration,
          status: payload.status ?? (isError ? "failed" : "completed"),
          content: payload.content ?? "",
          is_error: isError,
          duration_ms: payload.duration_ms ?? null,
          finished_at: payload.finished_at ?? null,
          truncated: payload.truncated ?? false,
        };
        patchAssistant(payload.message_id, (message) => ({
          ...message,
          streaming: true,
          reconnecting: false,
          cancelRequested: cancelRequestedRef.current,
          toolTrace: upsertToolTrace(message.toolTrace, entry),
        }));
        return;
      }

      if (event.event === "message.completed") {
        const payload = parse<CompletedPayload>(event);
        if (!payload || !acceptSeq(event, payload)) return;

        const finalMessage = payload.message;
        const serverTrace = normalizeToolTrace(
          finalMessage.metadata?.tool_trace,
        );
        ensureAssistantBubble(finalMessage.id, finalMessage.conversation_id);
        patchAssistant(finalMessage.id, (message) => ({
          ...toView(finalMessage),
          toolTrace: serverTrace ?? message.toolTrace,
          streaming: false,
          reconnecting: false,
          runStatus: "completed",
          failed: false,
          cancelRequested: false,
        }));
        runStatusRef.current = "completed";
        setRunStatus("completed");
        cancelRequestedRef.current = false;
        setCancelRequested(false);
        return;
      }

      if (event.event === "error") {
        const payload = parse<ErrorPayload>(event);
        if (!payload || !acceptSeq(event, payload)) return;

        const status: RunStatus =
          payload.code === "agent_cancelled"
            ? "cancelled"
            : payload.code === "agent_interrupted"
              ? "interrupted"
              : "failed";

        runStatusRef.current = status;
        setRunStatus(status);
        if (status !== "cancelled") {
          setError(
            payload.code === "agent_not_configured"
              ? NOT_CONFIGURED_MESSAGE
              : payload.detail || "The agent could not complete this turn.",
          );
        }

        setMessages((previous) =>
          previous.map((message) =>
            message.role === "assistant" &&
            (message.streaming || message.id === assistantIdRef.current)
              ? {
                  ...message,
                  streaming: false,
                  reconnecting: false,
                  runStatus: status,
                  failed: status === "failed",
                  status:
                    status === "cancelled"
                      ? "cancelled"
                      : status === "failed"
                        ? "failed"
                        : message.status,
                  toolTrace: finalizeRunningToolTrace(message.toolTrace),
                }
              : message,
          ),
        );
        if (payload.partial === false) {
          // Nothing was persisted for this turn: drop the empty placeholder.
          setMessages((previous) =>
            previous.filter(
              (message) =>
                !(
                  message.id === assistantIdRef.current &&
                  message.content === "" &&
                  !message.toolTrace?.length
                ),
            ),
          );
        }
        return;
      }

      if (event.event === "done") {
        const payload = parse<DonePayload>(event);
        const candidate = payload?.status ?? runStatusRef.current;
        const finalStatus: RunStatus = isTerminalStatus(candidate)
          ? candidate
          : "completed";

        setMessages((previous) =>
          previous.map((message) =>
            message.role === "assistant" &&
            (message.streaming || message.id === assistantIdRef.current)
              ? {
                  ...message,
                  metadata:
                    payload?.run_id &&
                    typeof message.metadata?.run_id !== "string"
                      ? { ...message.metadata, run_id: payload.run_id }
                      : message.metadata,
                  streaming: false,
                  reconnecting: false,
                  runStatus: finalStatus,
                  failed: finalStatus === "failed",
                  status:
                    finalStatus === "cancelled"
                      ? "cancelled"
                      : finalStatus === "failed"
                        ? "failed"
                        : message.status,
                  toolTrace: finalizeRunningToolTrace(message.toolTrace),
                }
              : message.reconnecting
                ? { ...message, reconnecting: false }
                : message,
          ),
        );
        finishRun(finalStatus);
        return;
      }
    },
    [
      acceptSeq,
      clearRetryTimer,
      ensureAssistantBubble,
      finishRun,
      patchAssistant,
      persistCursor,
      stopPolling,
    ],
  );

  const consumeRun = useCallback(
    async (response: Response, controller: AbortController, epoch: number) => {
      if (!response.body) {
        throw new ApiStreamError("The agent returned an empty stream.", 502);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SseParser();

      const dispatch = (events: SseMessage[]) => {
        for (const event of events) {
          if (controller.signal.aborted || epoch !== epochRef.current) return;
          handleEvent(event);
        }
      };

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          lastEventAtRef.current = Date.now();
          armWatchdog(epoch);
          dispatch(parser.push(decoder.decode(value, { stream: true })));
        }
        dispatch(parser.flush());
      } finally {
        try {
          reader.releaseLock();
        } catch {}
      }
    },
    [armWatchdog, handleEvent],
  );

  const handleRunGone = useCallback(() => {
    const runId = runIdRef.current;
    if (runId) staleRunIdsRef.current.add(runId);
    const conversationId = activeIdRef.current;
    if (conversationId) clearCursor(conversationId);
    stopLocalRun("closed");
    toast({
      title: "This reply has ended",
      description: "Reloading the saved conversation.",
    });
    if (conversationId) void loadMessagesRef.current(conversationId);
  }, [clearCursor, stopLocalRun]);

  const handleEventsExpired = useCallback(() => {
    const runId = runIdRef.current;
    if (runId) staleRunIdsRef.current.add(runId);
    const conversationId = activeIdRef.current;
    if (conversationId) clearCursor(conversationId);
    stopLocalRun("closed");
    toast({
      title: "History for this reply has expired",
      description: "Showing the final saved messages instead.",
    });
    if (conversationId) void loadMessagesRef.current(conversationId);
  }, [clearCursor, stopLocalRun]);

  const handleForbidden = useCallback(() => {
    stopLocalRun("closed");
    setError("You do not have access to this conversation.");
  }, [stopLocalRun]);

  const connect = useCallback(
    async (runId: string, afterSeq: number) => {
      if (!runId || terminalRef.current) return;

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const epoch = ++epochRef.current;

      if (
        connectionRef.current === "idle" ||
        connectionRef.current === "closed"
      ) {
        connectionRef.current = "connecting";
        setConnection("connecting");
      }

      lastEventAtRef.current = Date.now();
      armWatchdog(epoch);

      try {
        const response = await getStream(
          AGENT_ENDPOINTS.RUN_EVENTS(runId, afterSeq),
          { signal: controller.signal },
        );
        await consumeRun(response, controller, epoch);

        if (controller.signal.aborted || epoch !== epochRef.current) return;
        if (terminalRef.current) return;
        consecutiveFailuresRef.current += 1;
        scheduleReconnectRef.current();
      } catch (err) {
        if (controller.signal.aborted || epoch !== epochRef.current) return;
        if (terminalRef.current) return;

        if (err instanceof ApiStreamError) {
          if (err.status === 404) {
            handleRunGone();
            return;
          }
          if (err.status === 410) {
            handleEventsExpired();
            return;
          }
          if (err.status === 403) {
            handleForbidden();
            return;
          }
          if (err.status === 401) {
            stopLocalRun("closed");
            return;
          }
        }

        consecutiveFailuresRef.current += 1;
        scheduleReconnectRef.current();
      }
    },
    [
      armWatchdog,
      consumeRun,
      handleEventsExpired,
      handleForbidden,
      handleRunGone,
      stopLocalRun,
    ],
  );
  connectRef.current = connect;

  const pollRun = useCallback(async () => {
    const runId = runIdRef.current;
    if (!runId || terminalRef.current) {
      pollingRef.current = false;
      return;
    }

    try {
      const response = await api.get<AgentRun>(AGENT_ENDPOINTS.RUN(runId));
      if (!pollingRef.current || runIdRef.current !== runId) return;

      const run = response.data;
      if (!isActiveStatus(run.status)) {
        runStatusRef.current = run.status;
        setRunStatus(run.status);
        finishRun(run.status);
        return;
      }
      runStatusRef.current = run.status;
      setRunStatus(run.status);
    } catch (err) {
      if (!pollingRef.current || runIdRef.current !== runId) return;
      if (httpStatus(err) === 404) {
        handleRunGone();
        return;
      }
    }

    if (pollingRef.current) {
      pollTimerRef.current = window.setTimeout(() => {
        pollTimerRef.current = null;
        void pollRunRef.current();
      }, POLL_MS);
    }
  }, [finishRun, handleRunGone]);
  pollRunRef.current = pollRun;

  const startPolling = useCallback(() => {
    const runId = runIdRef.current;
    if (!runId || terminalRef.current || pollingRef.current) return;
    clearRetryTimer();
    clearWatchdog();
    pollingRef.current = true;
    connectionRef.current = "reconnecting";
    setConnection("reconnecting");
    void pollRunRef.current();
  }, [clearRetryTimer, clearWatchdog]);

  const scheduleReconnect = useCallback(() => {
    const runId = runIdRef.current;
    if (!runId || terminalRef.current) return;

    connectionRef.current = "reconnecting";
    setConnection("reconnecting");
    setMessages((previous) =>
      previous.map((message) =>
        message.role === "assistant" && message.streaming
          ? { ...message, reconnecting: true }
          : message,
      ),
    );

    clearRetryTimer();
    clearWatchdog();
    if (consecutiveFailuresRef.current >= MAX_RECONNECT_FAILURES) {
      startPolling();
      return;
    }

    const index = Math.min(
      Math.max(consecutiveFailuresRef.current - 1, 0),
      BACKOFF_MS.length - 1,
    );
    const delay = Math.round(BACKOFF_MS[index] * (0.8 + Math.random() * 0.4));

    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      const nextRunId = runIdRef.current;
      if (!nextRunId || terminalRef.current) return;
      void connectRef.current(nextRunId, lastSeqRef.current);
    }, delay);
  }, [clearRetryTimer, clearWatchdog, startPolling]);
  scheduleReconnectRef.current = scheduleReconnect;

  const loadMessages = useCallback(
    async (conversationId: string) => {
      setIsLoadingMessages(true);
      try {
        const response = await api.get<ConversationDetail>(
          AGENT_ENDPOINTS.CONVERSATION(conversationId),
        );
        if (activeIdRef.current !== conversationId) return;

        const detail = response.data;
        setConversations((previous) => upsertConversation(previous, detail));

        const activeRun: ActiveRun | null = detail.active_run ?? null;
        const runActive =
          activeRun !== null &&
          isActiveStatus(activeRun.status) &&
          !staleRunIdsRef.current.has(activeRun.id);

        if (activeRun && runActive) {
          const sameRun =
            runIdRef.current === activeRun.id &&
            !terminalRef.current &&
            abortRef.current !== null;

          if (sameRun) {
            if (activeRun.assistant_message_id) {
              ensureAssistantBubble(
                activeRun.assistant_message_id,
                conversationId,
              );
            }
            return;
          }

          let views = detail.messages.map(toView);
          const assistantId = activeRun.assistant_message_id;
          if (assistantId) {
            if (views.some((message) => message.id === assistantId)) {
              views = views.map((message) =>
                message.id === assistantId
                  ? { ...message, streaming: true }
                  : message,
              );
            } else {
              views = [
                ...views,
                {
                  id: assistantId,
                  conversation_id: conversationId,
                  role: "assistant",
                  content: "",
                  status: "completed",
                  metadata: {},
                  created_at: new Date().toISOString(),
                  streaming: true,
                },
              ];
            }
          }

          setMessages(views);
          assistantIdRef.current = assistantId;
          runIdRef.current = activeRun.id;
          lastSeqRef.current = 0;
          terminalRef.current = false;
          consecutiveFailuresRef.current = 0;
          runStatusRef.current = activeRun.status;
          setRunStatus(activeRun.status);
          setActiveRunId(activeRun.id);
          cursorKeyRef.current = cursorKey(conversationId);
          cancelRequestedRef.current = activeRun.cancel_requested_at !== null;
          setCancelRequested(cancelRequestedRef.current);
          sendingRef.current = true;
          setIsSending(true);
          connectionRef.current = "connecting";
          setConnection("connecting");
          // Assistant text is not persisted yet: replay from the beginning.
          void connect(activeRun.id, 0);
          return;
        }

        clearCursor(conversationId);
        setMessages(detail.messages.map(toView));
        assistantIdRef.current = null;
        runIdRef.current = null;
        lastSeqRef.current = 0;
        terminalRef.current = false;
        runStatusRef.current = null;
        setRunStatus(null);
        setActiveRunId(null);
        cancelRequestedRef.current = false;
        setCancelRequested(false);
        sendingRef.current = false;
        setIsSending(false);
        connectionRef.current = "idle";
        setConnection("idle");
      } catch (err) {
        if (activeIdRef.current === conversationId) {
          setError(getErrorMessage(err, "Failed to load conversation"));
        }
      } finally {
        setIsLoadingMessages(false);
      }
    },
    [clearCursor, connect, ensureAssistantBubble],
  );
  loadMessagesRef.current = loadMessages;

  const retryNow = useCallback(() => {
    const runId = runIdRef.current;
    if (!runId || terminalRef.current) return;
    clearRetryTimer();
    clearWatchdog();
    stopPolling();
    consecutiveFailuresRef.current = 0;
    void connectRef.current(runId, lastSeqRef.current);
  }, [clearRetryTimer, clearWatchdog, stopPolling]);

  const handleLegacyFailure = useCallback((err?: unknown) => {
    terminalRef.current = true;
    runIdRef.current = null;
    lastSeqRef.current = 0;
    runStatusRef.current = "failed";
    setRunStatus("failed");
    setActiveRunId(null);
    cancelRequestedRef.current = false;
    setCancelRequested(false);
    sendingRef.current = false;
    setIsSending(false);
    connectionRef.current = "closed";
    setConnection("closed");
    if (err) {
      setError(getErrorMessage(err, "Failed to reach the agent"));
    }
    setMessages((previous) =>
      previous.map((message) =>
        message.role === "assistant" &&
        (message.streaming || message.id === assistantIdRef.current)
          ? {
              ...message,
              streaming: false,
              reconnecting: false,
              failed: true,
              runStatus: "failed",
              toolTrace: finalizeRunningToolTrace(message.toolTrace),
            }
          : message,
      ),
    );
  }, []);

  const send = useCallback(
    async (rawText: string): Promise<SendResult> => {
      const text = rawText.trim();
      if (!text) return { ok: false };
      if (sendingRef.current) return { ok: false, draft: text };

      const existingRunId = runIdRef.current;
      if (existingRunId) {
        // Same tab already has a run: observe it instead of starting another.
        void connectRef.current(existingRunId, lastSeqRef.current);
        return { ok: false, draft: text };
      }

      setError(null);
      const stamp = Date.now();
      const conversationId = activeIdRef.current;
      const startedWithoutConversation = conversationId === null;
      const tempUserId = `temp-user-${stamp}`;
      const assistantId = `temp-assistant-${stamp}`;
      assistantIdRef.current = assistantId;

      sendingRef.current = true;
      setIsSending(true);
      terminalRef.current = false;
      runIdRef.current = null;
      lastSeqRef.current = 0;
      cursorKeyRef.current = null;
      runStatusRef.current = null;
      consecutiveFailuresRef.current = 0;
      setRunStatus(null);
      setActiveRunId(null);
      cancelRequestedRef.current = false;
      setCancelRequested(false);
      connectionRef.current = "connecting";
      setConnection("connecting");

      setMessages((previous) => [
        ...previous,
        {
          id: tempUserId,
          conversation_id: conversationId ?? "",
          role: "user",
          content: text,
          status: "completed",
          metadata: {},
          created_at: new Date().toISOString(),
        },
        {
          id: assistantId,
          conversation_id: conversationId ?? "",
          role: "assistant",
          content: "",
          status: "completed",
          metadata: {},
          created_at: new Date().toISOString(),
          streaming: true,
        },
      ]);

      const controller = new AbortController();
      abortRef.current = controller;
      const epoch = ++epochRef.current;
      lastEventAtRef.current = Date.now();
      armWatchdog(epoch);

      try {
        const response = await postStream(
          AGENT_ENDPOINTS.INVOKE,
          { conversation_id: conversationId, message: text, stream: true },
          { signal: controller.signal },
        );

        void consumeRun(response, controller, epoch)
          .then(() => {
            if (controller.signal.aborted || epoch !== epochRef.current) return;
            if (terminalRef.current) return;
            consecutiveFailuresRef.current += 1;
            if (runIdRef.current) {
              scheduleReconnectRef.current();
            } else {
              handleLegacyFailure();
            }
          })
          .catch((err) => {
            if (controller.signal.aborted || epoch !== epochRef.current) return;
            if (terminalRef.current) return;

            if (err instanceof ApiStreamError) {
              if (err.status === 404) {
                handleRunGone();
                return;
              }
              if (err.status === 410) {
                handleEventsExpired();
                return;
              }
              if (err.status === 403) {
                handleForbidden();
                return;
              }
            }

            consecutiveFailuresRef.current += 1;
            if (runIdRef.current) {
              scheduleReconnectRef.current();
            } else {
              handleLegacyFailure(err);
            }
          });

        return { ok: true };
      } catch (err) {
        if (controller.signal.aborted) {
          return { ok: false };
        }

        if (err instanceof ApiStreamError && err.status === 409) {
          abortRef.current = null;
          terminalRef.current = false;
          sendingRef.current = false;
          setIsSending(false);
          runIdRef.current = null;
          lastSeqRef.current = 0;
          cursorKeyRef.current = null;
          runStatusRef.current = null;
          setRunStatus(null);
          setActiveRunId(null);
          cancelRequestedRef.current = false;
          setCancelRequested(false);
          assistantIdRef.current = null;
          connectionRef.current = "idle";
          setConnection("idle");
          setMessages((previous) =>
            previous.filter(
              (message) =>
                message.id !== tempUserId && message.id !== assistantId,
            ),
          );

          toast({
            title: "A reply is still in progress",
            description:
              "Your message was not sent. Wait for it to finish or stop it, then try again.",
          });

          const nextRunId =
            typeof err.data?.run_id === "string" ? err.data.run_id : null;
          if (nextRunId) {
            runIdRef.current = nextRunId;
            lastSeqRef.current = 0;
            terminalRef.current = false;
            sendingRef.current = true;
            setIsSending(true);
            connectionRef.current = "connecting";
            setConnection("connecting");
            // This tab has no local text for the run, so replay from 0.
            void connect(nextRunId, 0);
          }

          return { ok: false, draft: text };
        }

        const message =
          err instanceof ApiStreamError && err.code === "agent_not_configured"
            ? NOT_CONFIGURED_MESSAGE
            : getErrorMessage(err, "Failed to reach the agent");
        setError(message);
        abortRef.current = null;
        terminalRef.current = true;
        sendingRef.current = false;
        setIsSending(false);
        runIdRef.current = null;
        runStatusRef.current = null;
        setRunStatus(null);
        setActiveRunId(null);
        connectionRef.current = "closed";
        setConnection("closed");

        if (startedWithoutConversation && activeIdRef.current === null) {
          setMessages((previous) =>
            previous.filter(
              (item) => item.id !== tempUserId && item.id !== assistantId,
            ),
          );
        } else {
          setMessages((previous) =>
            previous
              .map((item) =>
                item.id === assistantId
                  ? {
                      ...item,
                      streaming: false,
                      failed: true,
                      runStatus: "failed" as const,
                      toolTrace: finalizeRunningToolTrace(item.toolTrace),
                    }
                  : item,
              )
              .filter(
                (item) =>
                  !(
                    item.id === assistantId &&
                    item.content === "" &&
                    !item.toolTrace?.length
                  ),
              ),
          );
        }

        return { ok: false, draft: text };
      }
    },
    [
      armWatchdog,
      connect,
      consumeRun,
      handleEventsExpired,
      handleForbidden,
      handleLegacyFailure,
      handleRunGone,
    ],
  );

  const stop = useCallback(async () => {
    if (cancelRequestedRef.current) return;

    const runId = runIdRef.current;
    if (!runId) {
      // Legacy server (meta without run_id): abort is the only stop.
      closeSubscription();
      terminalRef.current = true;
      sendingRef.current = false;
      setIsSending(false);
      connectionRef.current = "closed";
      setConnection("closed");
      cancelRequestedRef.current = false;
      setCancelRequested(false);
      setMessages((previous) =>
        previous.map((message) =>
          message.role === "assistant" &&
          (message.streaming || message.id === assistantIdRef.current)
            ? {
                ...message,
                streaming: false,
                status: "cancelled",
                runStatus: "cancelled",
                failed: false,
                toolTrace: finalizeRunningToolTrace(message.toolTrace),
              }
            : message,
        ),
      );
      return;
    }

    cancelRequestedRef.current = true;
    setCancelRequested(true);
    setMessages((previous) =>
      previous.map((message) =>
        message.role === "assistant" && message.streaming
          ? { ...message, cancelRequested: true }
          : message,
      ),
    );

    try {
      await api.post(AGENT_ENDPOINTS.RUN_CANCEL(runId));
    } catch (err) {
      if (httpStatus(err) === 404) {
        // Already terminal; the subscription will observe done.
        return;
      }
      cancelRequestedRef.current = false;
      setCancelRequested(false);
      setMessages((previous) =>
        previous.map((message) =>
          message.cancelRequested
            ? { ...message, cancelRequested: false }
            : message,
        ),
      );
      toast({
        title: "Failed to stop the agent",
        description: getErrorMessage(err, "Please try again."),
        variant: "destructive",
      });
    }
  }, [closeSubscription]);

  const selectConversation = useCallback(
    (conversationId: string) => {
      if (activeIdRef.current === conversationId) return;

      closeSubscription();
      activeIdRef.current = conversationId;
      setActiveId(conversationId);
      setMessages([]);
      setError(null);
      runIdRef.current = null;
      lastSeqRef.current = 0;
      assistantIdRef.current = null;
      terminalRef.current = false;
      runStatusRef.current = null;
      setRunStatus(null);
      setActiveRunId(null);
      cancelRequestedRef.current = false;
      setCancelRequested(false);
      sendingRef.current = false;
      setIsSending(false);
      connectionRef.current = "idle";
      setConnection("idle");
      void loadMessages(conversationId);
    },
    [closeSubscription, loadMessages],
  );

  const startNewChat = useCallback(() => {
    closeSubscription();
    activeIdRef.current = null;
    setActiveId(null);
    setMessages([]);
    setError(null);
    runIdRef.current = null;
    lastSeqRef.current = 0;
    assistantIdRef.current = null;
    terminalRef.current = false;
    runStatusRef.current = null;
    setRunStatus(null);
    setActiveRunId(null);
    cancelRequestedRef.current = false;
    setCancelRequested(false);
    sendingRef.current = false;
    setIsSending(false);
    connectionRef.current = "idle";
    setConnection("idle");
  }, [closeSubscription]);

  useEffect(() => {
    let disposed = false;
    void (async () => {
      try {
        const list = await loadConversations();
        if (disposed || list.length === 0) return;
        activeIdRef.current = list[0].id;
        setActiveId(list[0].id);
        void loadMessages(list[0].id);
      } catch (err) {
        if (!disposed) {
          setError(getErrorMessage(err, "Failed to load conversations"));
        }
      } finally {
        if (!disposed) setIsLoadingConversations(false);
      }
    })();

    return () => {
      disposed = true;
    };
  }, [loadConversations, loadMessages]);

  useEffect(() => {
    return () => {
      closeSubscription();
    };
  }, [closeSubscription]);

  useEffect(() => {
    const handleOnline = () => {
      if (connectionRef.current === "reconnecting" || pollingRef.current) {
        retryNow();
      }
    };

    const handleVisibility = () => {
      if (document.visibilityState !== "visible") return;
      if (!runIdRef.current || terminalRef.current) return;
      const stale = Date.now() - lastEventAtRef.current > WATCHDOG_MS;
      if (
        connectionRef.current === "reconnecting" ||
        pollingRef.current ||
        (connectionRef.current === "open" && stale)
      ) {
        retryNow();
      }
    };

    window.addEventListener("online", handleOnline);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      window.removeEventListener("online", handleOnline);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [retryNow]);

  const renameConversation = useCallback(
    async (conversationId: string, title: string) => {
      const trimmed = title.trim();
      if (!trimmed) return;
      try {
        const response = await api.put<Conversation>(
          AGENT_ENDPOINTS.CONVERSATION(conversationId),
          { title: trimmed },
        );
        setConversations((previous) =>
          upsertConversation(previous, response.data),
        );
      } catch (err) {
        toast({
          title: "Failed to rename conversation",
          description: getErrorMessage(err),
          variant: "destructive",
        });
      }
    },
    [],
  );

  const deleteConversation = useCallback(
    async (conversationId: string) => {
      try {
        await api.delete(AGENT_ENDPOINTS.CONVERSATION(conversationId));
        setConversations((previous) =>
          previous.filter((item) => item.id !== conversationId),
        );
        clearCursor(conversationId);
        if (activeIdRef.current === conversationId) startNewChat();
        toast({ title: "Conversation deleted", variant: "success" });
      } catch (err) {
        const status = httpStatus(err);
        toast({
          title:
            status === 409
              ? "A reply is still in progress"
              : "Failed to delete conversation",
          description:
            status === 409
              ? "Stop it or wait for it to finish, then try again."
              : getErrorMessage(err),
          variant: "destructive",
        });
      }
    },
    [clearCursor, startNewChat],
  );

  const activeConversation =
    conversations.find((item) => item.id === activeId) ?? null;

  const isStreaming =
    !isTerminalStatus(runStatus) && (isSending || activeRunId !== null);

  return {
    conversations,
    activeId,
    activeConversation,
    messages,
    isLoadingConversations,
    isLoadingMessages,
    isStreaming,
    connectionState: connection,
    runStatus,
    cancelRequested,
    activeRunId,
    error,
    send,
    stop,
    selectConversation,
    startNewChat,
    renameConversation,
    deleteConversation,
  };
}
