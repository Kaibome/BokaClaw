"use client";

import { useId, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
} from "lucide-react";

import {
  parseSubagentResult,
  SubagentTraceRow,
} from "@/components/agent/subagent-trace";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import type { MessageKey } from "@/i18n/messages/en";
import { useI18n } from "@/i18n/provider";
import type { ToolTraceEntry, ToolTraceStatus } from "@/types/api";

const STATUS_KEYS: Record<ToolTraceStatus, MessageKey> = {
  running: "tool.status.running",
  completed: "tool.status.completed",
  failed: "tool.status.failed",
  aborted: "tool.status.aborted",
};

function StatusIcon({ status }: { status: ToolTraceStatus }) {
  if (status === "running") {
    return (
      <Loader2 className="size-3.5 shrink-0 animate-spin text-onSurface-default-tertiary" />
    );
  }
  if (status === "completed") {
    return (
      <Check className="size-3.5 shrink-0 text-onSurface-default-secondary" />
    );
  }
  return (
    <AlertCircle className="size-3.5 shrink-0 text-onSurface-danger-primary" />
  );
}

function argumentsText(entry: ToolTraceEntry): string | null {
  if (entry.arguments && Object.keys(entry.arguments).length > 0) {
    return JSON.stringify(entry.arguments, null, 2);
  }
  return entry.raw_arguments ?? null;
}

function ToolTraceRow({ entry }: { entry: ToolTraceEntry }) {
  const [open, setOpen] = useState(false);
  const { t } = useI18n();
  const args = argumentsText(entry);
  const output = entry.content ?? "";

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="rounded-lg border border-memBorder-primary bg-surface-default-secondary"
    >
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left">
        <StatusIcon status={entry.status} />
        <span className="truncate font-dm-mono text-[11px] text-onSurface-default-primary">
          {entry.name}
        </span>
        <span className="shrink-0 typo-caption-sm text-onSurface-default-tertiary">
          {t(STATUS_KEYS[entry.status])}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {typeof entry.duration_ms === "number" && (
            <span className="typo-caption-sm text-onSurface-default-tertiary">
              {entry.duration_ms} ms
            </span>
          )}
          <ChevronRight
            className={cn(
              "size-3.5 shrink-0 text-onSurface-default-tertiary transition-transform",
              open && "rotate-90",
            )}
          />
        </span>
      </CollapsibleTrigger>

      <CollapsibleContent className="border-t border-memBorder-primary px-2.5 py-2">
        {entry.arguments_error && (
          <p className="mb-2 typo-caption-sm text-onSurface-danger-primary">
            {entry.arguments_error}
          </p>
        )}

        <div className="flex flex-col gap-1">
          <span className="typo-caption-sm text-onSurface-default-tertiary">
            {t("tool.arguments")}
          </span>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-default-primary p-2 font-dm-mono text-[11px] text-onSurface-default-secondary">
            {args ?? t("tool.noArguments")}
          </pre>
        </div>

        <div className="mt-2 flex flex-col gap-1">
          <span className="typo-caption-sm text-onSurface-default-tertiary">
            {t("tool.output")}
          </span>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-default-primary p-2 font-dm-mono text-[11px] text-onSurface-default-secondary">
            {output ||
              (entry.status === "running"
                ? t("tool.waiting")
                : t("tool.noOutput"))}
          </pre>
        </div>

        {entry.truncated && (
          <p className="mt-2 typo-caption-sm text-onSurface-default-tertiary">
            {t("tool.truncated")}
          </p>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function ToolTrace({ entries }: { entries: ToolTraceEntry[] }) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();
  const { t } = useI18n();

  if (entries.length === 0) {
    return null;
  }

  // Collapsed, only the most recent call is visible; expansion reveals the
  // whole turn in order. New entries keep the newest row in view while a run
  // is streaming, so the trace stays useful without extra state.
  const latest = entries[entries.length - 1];
  const visible = expanded ? entries : [latest];

  return (
    <div className="flex w-full max-w-[85%] min-w-0 flex-col gap-1">
      {entries.length > 1 && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          aria-controls={listId}
          className="flex items-center gap-1 self-start rounded px-1 py-0.5 typo-caption-sm text-onSurface-default-tertiary transition-colors hover:text-onSurface-default-primary"
        >
          <ChevronDown
            className={cn(
              "size-3 shrink-0 transition-transform",
              expanded && "rotate-180",
            )}
          />
          {expanded
            ? t("tool.showLatest")
            : t("tool.showAll", { count: entries.length })}
        </button>
      )}

      <div id={listId} className="flex flex-col gap-1">
        {visible.map((entry) => {
          const subagent = parseSubagentResult(entry);
          return subagent ? (
            <SubagentTraceRow
              key={entry.tool_call_id}
              entry={entry}
              result={subagent}
            />
          ) : (
            <ToolTraceRow key={entry.tool_call_id} entry={entry} />
          );
        })}
      </div>
    </div>
  );
}
