"use client";

import { useState, type ReactNode } from "react";
import {
  AlertCircle,
  Bot,
  Check,
  ChevronRight,
  Loader2,
  Search,
} from "lucide-react";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn, formatCompactNumber, formatTokens } from "@/lib/utils";
import type {
  SubagentResult,
  SubagentStatus,
  ToolTraceEntry,
  ToolTraceStatus,
} from "@/types/api";

const SUBAGENT_STATUSES = new Set<SubagentStatus>([
  "completed",
  "partial",
  "failed",
  "timeout",
  "cancelled",
]);

const STATUS_LABELS: Record<SubagentStatus, string> = {
  completed: "Completed",
  partial: "Partial",
  failed: "Failed",
  timeout: "Timed out",
  cancelled: "Cancelled",
};

function StatusIcon({ status }: { status: ToolTraceStatus }) {
  if (status === "running") {
    return (
      <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin text-onSurface-default-tertiary" />
    );
  }
  if (status === "completed") {
    return (
      <Check className="mt-0.5 size-3.5 shrink-0 text-onSurface-default-secondary" />
    );
  }
  return (
    <AlertCircle className="mt-0.5 size-3.5 shrink-0 text-onSurface-danger-primary" />
  );
}

function SubagentIcon({ subagentType }: { subagentType: string }) {
  const Icon = subagentType.toLowerCase().includes("research") ? Search : Bot;
  return <Icon className="size-3.5 shrink-0 text-onSurface-default-tertiary" />;
}

function isSubagentResult(value: unknown): value is SubagentResult {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.subagent_id === "string" &&
    typeof record.subagent_type === "string" &&
    typeof record.status === "string" &&
    SUBAGENT_STATUSES.has(record.status as SubagentStatus)
  );
}

/**
 * Parse the tool output into a SubagentResult. Anything that is not a complete
 * envelope (a running call, a truncated result, a regular tool) returns null so
 * the caller keeps the generic JSON rendering.
 */
export function parseSubagentResult(
  entry: ToolTraceEntry,
): SubagentResult | null {
  const text = entry.content?.trim();
  if (!text || !text.startsWith("{")) return null;
  try {
    const value: unknown = JSON.parse(text);
    return isSubagentResult(value) ? value : null;
  } catch {
    return null;
  }
}

function taskText(entry: ToolTraceEntry): {
  title: string;
  subtitle: string | null;
} {
  const args = entry.arguments ?? {};
  const label = typeof args.label === "string" ? args.label.trim() : "";
  const goal = typeof args.goal === "string" ? args.goal.trim() : "";
  return {
    title: label || goal || "Subagent task",
    subtitle: label && goal ? goal : null,
  };
}

function totalTokens(usage: Record<string, number> | undefined): number | null {
  if (!usage) return null;
  if (typeof usage.total_tokens === "number" && usage.total_tokens > 0) {
    return usage.total_tokens;
  }
  const sum = (usage.prompt_tokens ?? 0) + (usage.completion_tokens ?? 0);
  return sum > 0 ? sum : null;
}

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mt-3 flex flex-col gap-1 first:mt-0">
      <span className="typo-caption-sm text-onSurface-default-tertiary">
        {label}
      </span>
      {children}
    </div>
  );
}

function ConfidenceBadge({ confidence }: { confidence?: string }) {
  if (!confidence) return null;
  return (
    <span
      className={cn(
        "inline-flex rounded border border-memBorder-primary px-1 py-0.5 typo-caption-sm uppercase",
        confidence === "low"
          ? "text-onSurface-default-tertiary"
          : "text-onSurface-default-secondary",
      )}
    >
      {confidence}
    </span>
  );
}

function FactsTable({ result }: { result: SubagentResult }) {
  const facts = result.facts ?? [];
  if (facts.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded bg-surface-default-primary p-2">
      <table className="w-full min-w-[360px] border-collapse text-left">
        <thead>
          <tr className="typo-caption-sm text-onSurface-default-tertiary">
            <th className="pb-1 pr-2 font-normal">CLAIM</th>
            <th className="pb-1 pr-2 font-normal">EVIDENCE</th>
            <th className="pb-1 font-normal">CONFIDENCE</th>
          </tr>
        </thead>
        <tbody>
          {facts.map((fact, index) => (
            <tr
              key={`${index}-${fact.claim}`}
              className="border-t border-memBorder-primary align-top"
            >
              <td className="py-1 pr-2 typo-body-xs text-onSurface-default-primary">
                {fact.claim}
              </td>
              <td className="py-1 pr-2 font-dm-mono text-[10px] break-words text-onSurface-default-secondary">
                {fact.evidence || "—"}
              </td>
              <td className="py-1">
                <ConfidenceBadge confidence={fact.confidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TextList({ items }: { items: string[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="flex list-disc flex-col gap-0.5 pl-4 typo-body-xs text-onSurface-default-secondary">
      {items.map((item, index) => (
        <li key={`${index}-${item}`} className="break-words">
          {item}
        </li>
      ))}
    </ul>
  );
}

export function SubagentTraceRow({
  entry,
  result,
}: {
  entry: ToolTraceEntry;
  result: SubagentResult;
}) {
  const [open, setOpen] = useState(false);
  const { title, subtitle } = taskText(entry);
  const report = result.report?.trim() ?? "";
  const tokens = totalTokens(result.usage);
  const statusLabel = STATUS_LABELS[result.status];
  const resultChars = entry.content?.length ?? 0;

  const meta = [
    result.turns ? `${result.turns} turns` : null,
    result.isolated_chars
      ? `${formatCompactNumber(result.isolated_chars)} chars isolated`
      : null,
    resultChars ? `${formatCompactNumber(resultChars)} result chars` : null,
    tokens !== null ? `${formatTokens(tokens)} tokens` : null,
  ].filter((item): item is string => item !== null);

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="rounded-lg border border-memBorder-primary bg-surface-default-secondary"
    >
      <CollapsibleTrigger className="flex w-full items-start gap-2 px-2.5 py-1.5 text-left">
        <StatusIcon status={entry.status} />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 flex-wrap items-center gap-2">
            <SubagentIcon subagentType={result.subagent_type} />
            <span className="truncate text-[12px] text-onSurface-default-primary">
              {title}
            </span>
            <span className="shrink-0 rounded border border-memBorderPrimary px-1 py-0.5 typo-caption-sm text-onSurface-default-tertiary">
              SUBAGENT
            </span>
            <span className="shrink-0 typo-caption-sm text-onSurface-default-tertiary">
              {statusLabel}
            </span>
          </span>
          {subtitle ? (
            <span className="block truncate typo-caption-sm text-onSurface-default-tertiary">
              {subtitle}
            </span>
          ) : null}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {typeof entry.duration_ms === "number" && (
            <span className="typo-caption-sm text-onSurface-default-tertiary">
              {entry.duration_ms} ms
            </span>
          )}
          <ChevronRight
            className={cn(
              "mt-0.5 size-3.5 shrink-0 text-onSurface-default-tertiary transition-transform",
              open && "rotate-90",
            )}
          />
        </span>
      </CollapsibleTrigger>

      <CollapsibleContent className="border-t border-memBorder-primary px-2.5 py-2">
        {meta.length > 0 ? (
          <div className="mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 typo-caption-sm text-onSurface-default-tertiary">
            {meta.map((item) => (
              <span key={item}>{item}</span>
            ))}
          </div>
        ) : null}

        {result.error ? (
          <p className="mb-2 typo-caption-sm text-onSurface-danger-primary">
            {result.error}
          </p>
        ) : null}

        {report ? (
          <Section label="REPORT">
            <p className="whitespace-pre-wrap break-words typo-body-xs leading-5 text-onSurface-default-primary">
              {report}
            </p>
          </Section>
        ) : result.data ? (
          <Section label="DATA">
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-default-primary p-2 font-dm-mono text-[11px] text-onSurface-default-secondary">
              {JSON.stringify(result.data, null, 2)}
            </pre>
          </Section>
        ) : null}

        {(result.facts?.length ?? 0) > 0 ? (
          <Section label="FACTS">
            <FactsTable result={result} />
          </Section>
        ) : null}

        {(result.inferences?.length ?? 0) > 0 ? (
          <Section label="INFERENCES">
            <TextList items={result.inferences ?? []} />
          </Section>
        ) : null}

        {(result.unknowns?.length ?? 0) > 0 ? (
          <Section label="UNKNOWNS">
            <TextList items={result.unknowns ?? []} />
          </Section>
        ) : null}

        {(result.sources?.length ?? 0) > 0 ? (
          <Section label="SOURCES">
            <TextList items={result.sources ?? []} />
          </Section>
        ) : null}

        {entry.truncated || result.truncated ? (
          <p className="mt-2 typo-caption-sm text-onSurface-default-tertiary">
            {result.truncated
              ? "The subagent report was truncated to the configured result limit."
              : "Output truncated for display."}
          </p>
        ) : null}
      </CollapsibleContent>
    </Collapsible>
  );
}
