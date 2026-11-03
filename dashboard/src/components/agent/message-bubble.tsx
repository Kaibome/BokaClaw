import { AlertCircle } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ChatMessageView } from "@/hooks/use-agent-chat";

function TypingDots() {
  return (
    <span
      className="inline-flex items-center py-1 text-onSurface-default-tertiary"
      aria-label="Agent is typing"
    >
      <span className="dot" />
      <span className="dot" />
      <span className="dot" />
    </span>
  );
}

export function MessageBubble({ message }: { message: ChatMessageView }) {
  const isUser = message.role === "user";

  return (
    <div
      className={cn(
        "flex flex-col gap-1",
        isUser ? "items-end" : "items-start",
      )}
    >
      <div
        className={cn(
          "max-w-[85%] whitespace-pre-wrap break-words rounded-xl border border-memBorder-primary px-3 py-2 text-sm text-onSurface-default-primary",
          isUser
            ? "bg-surface-default-secondary"
            : "bg-surface-default-primary",
        )}
      >
        {message.content ? (
          <span>{message.content}</span>
        ) : message.streaming ? (
          <TypingDots />
        ) : null}
        {message.streaming && message.content && (
          <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-pulse bg-onSurface-default-primary align-middle" />
        )}
      </div>

      {message.status === "cancelled" && (
        <span className="typo-caption-sm text-onSurface-default-tertiary">
          STOPPED
        </span>
      )}
      {message.failed && (
        <span className="flex items-center gap-1 typo-caption-sm text-onSurface-danger-primary">
          <AlertCircle className="size-3" />
          FAILED
        </span>
      )}
    </div>
  );
}
