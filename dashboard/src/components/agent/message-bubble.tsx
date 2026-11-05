import { AlertCircle, RotateCcw } from "lucide-react";

import { RunIdBadge } from "@/components/agent/run-id-badge";
import { ToolTrace } from "@/components/agent/tool-trace";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ChatMessageView } from "@/hooks/use-agent-chat";
import type { RunUsage } from "@/types/api";

function formatTokens(value: number): string {
  if (value >= 1_000_000) {
    const millions = value / 1_000_000;
    return `${millions >= 10 ? Math.round(millions) : millions.toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (value >= 1_000) {
    const thousands = value / 1_000;
    return `${thousands >= 100 ? Math.round(thousands) : thousands.toFixed(1).replace(/\.0$/, "")}k`;
  }
  return String(value);
}

function readUsage(metadata: ChatMessageView["metadata"]): RunUsage | null {
  const value = metadata?.usage;
  if (!value || typeof value !== "object") return null;
  const usage = value as Partial<RunUsage>;
  return typeof usage.coverage === "string" ? (usage as RunUsage) : null;
}

function usageLabel(usage: RunUsage): string | null {
  if (usage.coverage === "pending" || usage.coverage === "unavailable") {
    return "tokens —";
  }
  if (usage.coverage === "unknown") {
    return "tokens unknown";
  }
  if (usage.coverage === "none") {
    return "0 tokens";
  }
  if (usage.total_tokens === null || usage.total_tokens === undefined) {
    return "tokens —";
  }
  const total = formatTokens(usage.total_tokens);
  return usage.coverage === "partial" ? `≥ ${total} tokens` : `${total} tokens`;
}

function usageTitle(usage: RunUsage): string {
  const lines = [
    `input ${usage.input_tokens ?? "?"} · output ${usage.output_tokens ?? "?"} · ${usage.model_calls} calls`,
  ];
  for (const [source, detail] of Object.entries(usage.sources ?? {})) {
    if (!detail.calls) continue;
    const unknown = detail.unknown_calls
      ? ` (${detail.unknown_calls} unknown)`
      : "";
    lines.push(
      `${source}: in ${detail.input_tokens} / out ${detail.output_tokens}${unknown}`,
    );
  }
  if (usage.coverage === "partial") {
    lines.push("Some calls did not report usage; the total is a lower bound.");
  }
  return lines.join("\n");
}

function UsageBadge({ usage }: { usage: RunUsage }) {
  const label = usageLabel(usage);
  if (!label) return null;
  return (
    <span
      title={usageTitle(usage)}
      className="inline-flex items-center rounded-md px-1.5 py-0.5 typo-caption-sm text-onSurface-default-tertiary"
    >
      {label}
    </span>
  );
}

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
  const runId =
    typeof message.metadata?.run_id === "string"
      ? message.metadata.run_id
      : null;
  const usage = isUser ? null : readUsage(message.metadata);
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

      {!isUser && (runId || usage) ? (
        <div className="flex flex-wrap items-center gap-0.5">
          {runId ? <RunIdBadge runId={runId} /> : null}
          {usage ? <UsageBadge usage={usage} /> : null}
        </div>
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
