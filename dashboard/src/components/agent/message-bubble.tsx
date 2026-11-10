"use client";

import { AlertCircle, RotateCcw, Scissors } from "lucide-react";

import { RunIdBadge } from "@/components/agent/run-id-badge";
import { MarkdownContent } from "@/components/agent/markdown-content";
import { ToolTrace } from "@/components/agent/tool-trace";
import { UserQuestionCard } from "@/components/agent/user-question";
import { Button } from "@/components/ui/button";
import { cn, formatTokens } from "@/lib/utils";
import type { ChatMessageView } from "@/hooks/use-agent-chat";
import type { MessageKey } from "@/i18n/messages/en";
import { useI18n, type I18nContextValue } from "@/i18n/provider";
import type { CompressionInfo, RunUsage } from "@/types/api";

type Translate = I18nContextValue["t"];

function readUsage(metadata: ChatMessageView["metadata"]): RunUsage | null {
  const value = metadata?.usage;
  if (!value || typeof value !== "object") return null;
  const usage = value as Partial<RunUsage>;
  return typeof usage.coverage === "string" ? (usage as RunUsage) : null;
}

const SOURCE_KEYS: Record<string, MessageKey> = {
  agent_loop: "agent.usage.source.agentLoop",
  compression: "agent.usage.source.compression",
  memory_llm: "agent.usage.source.memoryLlm",
  memory_embedding: "agent.usage.source.memoryEmbedding",
  subagents: "agent.usage.source.subagents",
};

function usageLabel(usage: RunUsage, t: Translate): string | null {
  if (usage.coverage === "pending" || usage.coverage === "unavailable") {
    return t("agent.usage.pending");
  }
  if (usage.coverage === "unknown") {
    return t("agent.usage.unknown");
  }
  if (usage.coverage === "none") {
    return t("agent.usage.zero");
  }
  if (usage.total_tokens === null || usage.total_tokens === undefined) {
    return t("agent.usage.pending");
  }
  const total = formatTokens(usage.total_tokens);
  return usage.coverage === "partial"
    ? t("agent.usage.partial", { total })
    : t("agent.usage.exact", { total });
}

function usageTitle(usage: RunUsage, t: Translate): string {
  const lines = [
    t("agent.usage.summary", {
      input: usage.input_tokens ?? "?",
      output: usage.output_tokens ?? "?",
      calls: usage.model_calls,
    }),
  ];
  if (usage.cache_hit_rate !== null && usage.cache_hit_rate !== undefined) {
    const cached = formatTokens(usage.cached_tokens ?? 0);
    const missed = formatTokens(usage.cache_miss_tokens ?? 0);
    lines.push(
      t("agent.usage.cache", {
        percent: Math.round(usage.cache_hit_rate * 100),
        cached,
        missed,
      }),
    );
  }
  for (const [source, detail] of Object.entries(usage.sources ?? {})) {
    if (!detail.calls) continue;
    const unknown = detail.unknown_calls
      ? t("agent.usage.unknownCalls", { count: detail.unknown_calls })
      : "";
    const sourceKey = SOURCE_KEYS[source];
    const label = sourceKey ? t(sourceKey) : source;
    lines.push(
      t("agent.usage.sourceLine", {
        label,
        tokens: formatTokens(detail.total_tokens),
        unknown,
      }),
    );
  }
  if (usage.coverage === "partial") {
    lines.push(t("agent.usage.lowerBound"));
  }
  return lines.join("\n");
}

function UsageBadge({ usage }: { usage: RunUsage }) {
  const { t } = useI18n();
  const label = usageLabel(usage, t);
  if (!label) return null;
  return (
    <span
      title={usageTitle(usage, t)}
      className="inline-flex items-center rounded-md px-1.5 py-0.5 typo-caption-sm text-onSurface-default-tertiary"
    >
      {label}
    </span>
  );
}

function compressionTitle(compression: CompressionInfo, t: Translate): string {
  const { iteration, stats, count } = compression;
  const lines = [
    t("agent.compression.detail", {
      iteration: iteration || "?",
      before: formatTokens(stats.before_tokens ?? 0),
      after: formatTokens(stats.after_tokens ?? 0),
      dropped: stats.dropped_messages ?? 0,
    }),
  ];
  if (stats.summarized) lines.push(t("agent.compression.summarized"));
  if (stats.fallback) lines.push(t("agent.compression.fallback"));
  if (stats.model_calls) {
    lines.push(t("agent.compression.calls", { count: stats.model_calls }));
  }
  if (count > 1) lines.push(t("agent.compression.count", { count }));
  return lines.join("\n");
}

function CompressionBadge({ compression }: { compression: CompressionInfo }) {
  const { t } = useI18n();
  const label =
    compression.count > 1
      ? t("agent.compression.badgeCount", { count: compression.count })
      : t("agent.compression.badge");
  return (
    <span
      title={compressionTitle(compression, t)}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 typo-caption-sm text-onSurface-default-tertiary"
    >
      <Scissors className="size-3" />
      {label}
    </span>
  );
}

function TypingDots() {
  const { t } = useI18n();
  return (
    <span
      className="inline-flex items-center py-1 text-onSurface-default-tertiary"
      aria-label={t("agent.typing")}
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
  onAnswerQuestion,
}: {
  message: ChatMessageView;
  onRetry?: (message: ChatMessageView) => void;
  onAnswerQuestion?: (
    questionId: string,
    answer: string,
  ) => Promise<void> | void;
}) {
  const { t } = useI18n();
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
  const hasPendingQuestion =
    !isUser &&
    (message.questions?.some(
      (question) =>
        question.status === "pending" || question.status === "submitting",
    ) ??
      false);

  return (
    <div
      className={cn(
        "flex flex-col gap-1",
        isUser ? "items-end" : "items-start",
      )}
    >
      <div
        className={cn(
          "max-w-[85%] break-words rounded-xl border border-memBorder-primary px-3 py-2 text-sm text-onSurface-default-primary",
          isUser
            ? "whitespace-pre-wrap bg-surface-default-secondary"
            : "bg-surface-default-primary",
        )}
      >
        {message.content ? (
          isUser ? (
            <span>{message.content}</span>
          ) : (
            <MarkdownContent content={message.content} />
          )
        ) : message.streaming && !isReconnecting && !hasPendingQuestion ? (
          <TypingDots />
        ) : null}
        {message.streaming && !isReconnecting && message.content && (
          <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-pulse bg-onSurface-default-primary align-middle" />
        )}
      </div>

      {!isUser && message.questions?.length && onAnswerQuestion ? (
        <UserQuestionCard
          questions={message.questions}
          onAnswer={onAnswerQuestion}
        />
      ) : null}

      {!isUser && message.toolTrace?.length ? (
        <ToolTrace entries={message.toolTrace} />
      ) : null}

      {!isUser && (runId || usage || message.compression) ? (
        <div className="flex flex-wrap items-center gap-0.5">
          {runId ? <RunIdBadge runId={runId} /> : null}
          {usage ? <UsageBadge usage={usage} /> : null}
          {message.compression ? (
            <CompressionBadge compression={message.compression} />
          ) : null}
        </div>
      ) : null}

      {isReconnecting && (
        <span className="typo-caption-sm text-onSurface-default-tertiary">
          {t("agent.status.reconnecting")}
        </span>
      )}
      {isStopping && (
        <span className="typo-caption-sm text-onSurface-default-tertiary">
          {t("agent.status.stopping")}
        </span>
      )}
      {isInterrupted && (
        <span className="flex items-center gap-1 typo-caption-sm text-onSurface-danger-primary">
          <AlertCircle className="size-3" />
          {t("agent.status.interrupted")}
        </span>
      )}
      {isCancelled && (
        <span className="typo-caption-sm text-onSurface-default-tertiary">
          {t("agent.status.stopped")}
        </span>
      )}
      {isFailed && (
        <span className="flex items-center gap-1 typo-caption-sm text-onSurface-danger-primary">
          <AlertCircle className="size-3" />
          {t("agent.status.failed")}
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
          {t("agent.retry")}
        </Button>
      )}
    </div>
  );
}
