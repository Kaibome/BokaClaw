import { AlertCircle, RotateCcw } from "lucide-react";

import { ToolTrace } from "@/components/agent/tool-trace";
import { Button } from "@/components/ui/button";
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

export function MessageBubble({
  message,
  onRetry,
}: {
  message: ChatMessageView;
  onRetry?: (message: ChatMessageView) => void;
}) {
  const isUser = message.role === "user";
  const isInterrupted = !isUser && message.runStatus === "interrupted";
  const isFailed =
    !isUser &&
    !isInterrupted &&
    (message.runStatus === "failed" || message.failed === true);
  const isCancelled =
    !isUser &&
    !isInterrupted &&
    (message.runStatus === "cancelled" || message.status === "cancelled");
  const isReconnecting = message.reconnecting === true;
  const isStopping = message.cancelRequested === true && message.streaming;
  const canRetry =
    !isUser &&
    !message.streaming &&
    !isReconnecting &&
    (isInterrupted || isFailed || isCancelled) &&
    typeof onRetry === "function";

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
        ) : message.streaming && !isReconnecting ? (
          <TypingDots />
        ) : null}
        {message.streaming && !isReconnecting && message.content && (
          <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-pulse bg-onSurface-default-primary align-middle" />
        )}
      </div>

      {!isUser && message.toolTrace?.length ? (
        <ToolTrace entries={message.toolTrace} />
      ) : null}

      {isReconnecting && (
        <span className="typo-caption-sm text-onSurface-default-tertiary">
          RECONNECTING…
        </span>
      )}
      {isStopping && (
        <span className="typo-caption-sm text-onSurface-default-tertiary">
          STOPPING…
        </span>
      )}
      {isInterrupted && (
        <span className="flex items-center gap-1 typo-caption-sm text-onSurface-danger-primary">
          <AlertCircle className="size-3" />
          INTERRUPTED
        </span>
      )}
      {isCancelled && (
        <span className="typo-caption-sm text-onSurface-default-tertiary">
          STOPPED
        </span>
      )}
      {isFailed && (
        <span className="flex items-center gap-1 typo-caption-sm text-onSurface-danger-primary">
          <AlertCircle className="size-3" />
          FAILED
        </span>
      )}

      {canRetry && (
        <Button
          type="button"
          variant="ghost"
          size="xs"
          className="h-6 gap-1 px-1.5 text-onSurface-default-secondary"
          onClick={() => onRetry?.(message)}
        >
          <RotateCcw className="size-3" />
          Retry
        </Button>
      )}
    </div>
  );
}
