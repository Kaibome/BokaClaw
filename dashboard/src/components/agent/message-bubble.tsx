import { AlertCircle, RotateCcw } from "lucide-react";

import { RunIdBadge } from "@/components/agent/run-id-badge";
import { ToolTrace } from "@/components/agent/tool-trace";
import { Button } from "@/components/ui/button";
import { cn, formatTokens } from "@/lib/utils";
import type { ChatMessageView } from "@/hooks/use-agent-chat";
import type { RunUsage } from "@/types/api";

function readUsage(metadata: ChatMessageView["metadata"]): RunUsage | null {
  const value = metadata?.usage;
  if (!value || typeof value !== "object") return null;
  const usage = value as Partial<RunUsage>;
  return typeof usage.coverage === "string" ? (usage as RunUsage) : null;
}

const USAGE_SUFFIX = "tokens cost this run";

const SOURCE_LABELS: Record<string, string> = {
  agent_loop: "Agent loop",
  compression: "Compression",
  memory_llm: "Memory LLM",
  memory_embedding: "Memory embedding",
  subagents: "Subagents",
};

function usageLabel(usage: RunUsage): string | null {
  if (usage.coverage === "pending" || usage.coverage === "unavailable") {
    return `${USAGE_SUFFIX} —`;
  }
  if (usage.coverage === "unknown") {
    return `${USAGE_SUFFIX} unknown`;
  }
  if (usage.coverage === "none") {
    return `0 ${USAGE_SUFFIX}`;
  }
  if (usage.total_tokens === null || usage.total_tokens === undefined) {
    return `${USAGE_SUFFIX} —`;
  }
  const total = formatTokens(usage.total_tokens);
  return usage.coverage === "partial"
    ? `≥ ${total} ${USAGE_SUFFIX}`
    : `${total} ${USAGE_SUFFIX}`;
}

function usageTitle(usage: RunUsage): string {
  const lines = [
    `input ${usage.input_tokens ?? "?"} · output ${usage.output_tokens ?? "?"} · ${usage.model_calls} calls`,
  ];
  if (usage.cache_hit_rate !== null && usage.cache_hit_rate !== undefined) {
    const cached = formatTokens(usage.cached_tokens ?? 0);
    const missed = formatTokens(usage.cache_miss_tokens ?? 0);
    lines.push(
      `cache hit ${Math.round(usage.cache_hit_rate * 100)}% · ${cached} cached / ${missed} uncached`,
    );
  }
  for (const [source, detail] of Object.entries(usage.sources ?? {})) {
    if (!detail.calls) continue;
    const unknown = detail.unknown_calls
      ? ` (${detail.unknown_calls} unknown)`
      : "";
    const label = SOURCE_LABELS[source] ?? source;
    lines.push(
      `${label}: ${formatTokens(detail.total_tokens)} tokens${unknown}`,
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
