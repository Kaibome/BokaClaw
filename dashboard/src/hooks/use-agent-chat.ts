"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { ApiStreamError, api, postStream } from "@/utils/api";
import { AGENT_ENDPOINTS } from "@/utils/api-endpoints";
import { SseParser, type SseMessage } from "@/utils/sse";
import type {
  Conversation,
  ConversationDetail,
  ChatMessage,
} from "@/types/api";

export interface ChatMessageView extends ChatMessage {
  streaming?: boolean;
  failed?: boolean;
}

interface MetaPayload {
  conversation: Conversation;
  user_message: ChatMessage;
}

interface DeltaPayload {
  message_id: string;
  delta: string;
}

interface CompletedPayload {
  message: ChatMessage;
}

interface ErrorPayload {
  code?: string;
  detail?: string;
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
  return { ...message };
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
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const activeIdRef = useRef<string | null>(null);
  const streamingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const assistantIdRef = useRef<string | null>(null);

  const loadConversations = useCallback(async () => {
    const response = await api.get<Conversation[]>(
      AGENT_ENDPOINTS.CONVERSATIONS,
    );
    const list = Array.isArray(response.data) ? response.data : [];
    setConversations(list);
    return list;
  }, []);

  const loadMessages = useCallback(async (conversationId: string) => {
    setIsLoadingMessages(true);
    try {
      const response = await api.get<ConversationDetail>(
        AGENT_ENDPOINTS.CONVERSATION(conversationId),
      );
      if (activeIdRef.current !== conversationId) return;
      setMessages(response.data.messages.map(toView));
      setConversations((previous) =>
        upsertConversation(previous, response.data),
      );
    } catch (err) {
      if (activeIdRef.current === conversationId) {
        setError(getErrorMessage(err, "Failed to load conversation"));
      }
    } finally {
      setIsLoadingMessages(false);
    }
  }, []);

  const selectConversation = useCallback(
    (conversationId: string) => {
      if (activeIdRef.current === conversationId) return;
      abortRef.current?.abort();
      activeIdRef.current = conversationId;
      setActiveId(conversationId);
      setMessages([]);
      setError(null);
      void loadMessages(conversationId);
    },
    [loadMessages],
  );

  const startNewChat = useCallback(() => {
    abortRef.current?.abort();
    activeIdRef.current = null;
    setActiveId(null);
    setMessages([]);
    setError(null);
  }, []);

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
      abortRef.current?.abort();
    };
  }, [loadConversations, loadMessages]);

  const handleEvent = useCallback((event: SseMessage) => {
    if (event.event === "meta") {
      const payload = parse<MetaPayload>(event);
      if (!payload) return;
      activeIdRef.current = payload.conversation.id;
      setActiveId(payload.conversation.id);
      setConversations((previous) =>
        upsertConversation(previous, payload.conversation),
      );
      setMessages((previous) =>
        previous.map((message) =>
          message.role === "user" && message.id.startsWith("temp-")
            ? toView(payload.user_message)
            : message,
        ),
      );
      return;
    }

    if (event.event === "message.delta") {
      const payload = parse<DeltaPayload>(event);
      if (!payload) return;
      const previousId = assistantIdRef.current;
      if (previousId && previousId !== payload.message_id) {
        assistantIdRef.current = payload.message_id;
        setMessages((previous) =>
          previous.map((message) =>
            message.id === previousId
              ? { ...message, id: payload.message_id }
              : message,
          ),
        );
      }
      setMessages((previous) =>
        previous.map((message) =>
          message.id === assistantIdRef.current
            ? { ...message, content: message.content + payload.delta }
            : message,
        ),
      );
      return;
    }

    if (event.event === "message.completed") {
      const payload = parse<CompletedPayload>(event);
      if (!payload) return;
      setMessages((previous) =>
        previous.map((message) =>
          message.id === assistantIdRef.current
            ? { ...toView(payload.message), streaming: false }
            : message,
        ),
      );
      return;
    }

    if (event.event === "error") {
      const payload = parse<ErrorPayload>(event);
      setError(
        payload?.code === "agent_not_configured"
          ? NOT_CONFIGURED_MESSAGE
          : payload?.detail || "The agent could not complete this turn.",
      );
      setMessages((previous) =>
        previous.map((message) =>
          message.id === assistantIdRef.current
            ? { ...message, streaming: false, failed: true }
            : message,
        ),
      );
      return;
    }

    if (event.event === "done") {
      setMessages((previous) =>
        previous.map((message) =>
          message.id === assistantIdRef.current
            ? { ...message, streaming: false }
            : message,
        ),
      );
    }
  }, []);

  const send = useCallback(
    async (rawText: string) => {
      const text = rawText.trim();
      if (!text || streamingRef.current) return;

      const controller = new AbortController();
      abortRef.current = controller;
      streamingRef.current = true;
      setIsStreaming(true);
      setError(null);

      const stamp = Date.now();
      const conversationId = activeIdRef.current;
      const startedWithoutConversation = conversationId === null;
      const tempUserId = `temp-user-${stamp}`;
      const assistantId = `temp-assistant-${stamp}`;
      assistantIdRef.current = assistantId;

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

      try {
        const response = await postStream(
          AGENT_ENDPOINTS.INVOKE,
          { conversation_id: conversationId, message: text, stream: true },
          { signal: controller.signal },
        );

        if (!response.body) {
          throw new ApiStreamError("The agent returned an empty stream.", 502);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const parser = new SseParser();

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          for (const event of parser.push(
            decoder.decode(value, { stream: true }),
          )) {
            handleEvent(event);
          }
        }
        for (const event of parser.flush()) handleEvent(event);
      } catch (err) {
        if (controller.signal.aborted) {
          setMessages((previous) =>
            previous.map((message) =>
              message.id === assistantIdRef.current
                ? { ...message, streaming: false, status: "cancelled" }
                : message,
            ),
          );
        } else {
          const message =
            err instanceof ApiStreamError && err.code === "agent_not_configured"
              ? NOT_CONFIGURED_MESSAGE
              : getErrorMessage(err, "Failed to reach the agent");
          setError(message);
          // A 503/connection failure before `meta` means nothing was persisted,
          // so drop the optimistic bubbles instead of leaving a dangling turn.
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
                  item.id === assistantIdRef.current
                    ? { ...item, streaming: false, failed: true }
                    : item,
                )
                .filter(
                  (item) =>
                    !(
                      item.id === assistantIdRef.current && item.content === ""
                    ),
                ),
            );
          }
        }
      } finally {
        abortRef.current = null;
        streamingRef.current = false;
        setIsStreaming(false);
        void loadConversations().catch(() => {});
      }
    },
    [handleEvent, loadConversations],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

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
        if (activeIdRef.current === conversationId) startNewChat();
        toast({ title: "Conversation deleted", variant: "success" });
      } catch (err) {
        toast({
          title: "Failed to delete conversation",
          description: getErrorMessage(err),
          variant: "destructive",
        });
      }
    },
    [startNewChat],
  );

  const activeConversation =
    conversations.find((item) => item.id === activeId) ?? null;

  return {
    conversations,
    activeId,
    activeConversation,
    messages,
    isLoadingConversations,
    isLoadingMessages,
    isStreaming,
    error,
    send,
    stop,
    selectConversation,
    startNewChat,
    renameConversation,
    deleteConversation,
  };
}
