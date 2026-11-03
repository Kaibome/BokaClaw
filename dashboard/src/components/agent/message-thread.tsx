"use client";

import { useEffect, useRef } from "react";
import { AlertCircle } from "lucide-react";

import { AgentEmptyState } from "@/components/agent/agent-empty-state";
import { MessageBubble } from "@/components/agent/message-bubble";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import type { ChatMessageView } from "@/hooks/use-agent-chat";

interface MessageThreadProps {
  conversationId: string | null;
  messages: ChatMessageView[];
  isLoading: boolean;
  error: string | null;
}

function LoadingRows() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-14 w-3/5" />
      <Skeleton className="ml-auto h-10 w-2/5" />
      <Skeleton className="h-20 w-4/5" />
    </div>
  );
}

export function MessageThread({
  conversationId,
  messages,
  isLoading,
  error,
}: MessageThreadProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  useEffect(() => {
    stickToBottomRef.current = true;
  }, [conversationId]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;

    const handleScroll = () => {
      stickToBottomRef.current =
        viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 80;
    };

    viewport.addEventListener("scroll", handleScroll, { passive: true });
    return () => viewport.removeEventListener("scroll", handleScroll);
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !stickToBottomRef.current) return;
    viewport.scrollTop = viewport.scrollHeight;
  }, [messages]);

  return (
    <ScrollArea viewportRef={viewportRef} className="min-h-0 flex-1">
      <div className="flex min-h-full flex-col gap-4 px-4 py-4">
        {isLoading ? (
          <LoadingRows />
        ) : messages.length === 0 ? (
          <div className="flex flex-1 items-center justify-center">
            <AgentEmptyState
              title={
                conversationId ? "No messages yet" : "Start a conversation"
              }
              description={
                conversationId
                  ? "Send a message to continue this conversation."
                  : "Ask the agent anything. Your conversation is saved and will appear on the left."
              }
            />
          </div>
        ) : (
          messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))
        )}

        {error && (
          <div className="flex items-start gap-2 rounded-lg border border-memBorder-primary bg-surface-default-secondary px-3 py-2 text-sm text-onSurface-danger-primary">
            <AlertCircle className="mt-0.5 size-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}
      </div>
    </ScrollArea>
  );
}
