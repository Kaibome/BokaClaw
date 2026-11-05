"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUp, Loader2, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { formatTokens } from "@/lib/utils";
import type { SendResult } from "@/hooks/use-agent-chat";
import type { ContextUsage } from "@/types/api";

interface ChatComposerProps {
  onSend: (message: string) => Promise<SendResult> | void;
  onStop: () => void;
  isStreaming: boolean;
  cancelRequested: boolean;
  contextUsage?: ContextUsage | null;
}

const MAX_HEIGHT = 200;
const WARNING_THRESHOLD = 90;

function ContextMeter({ usage }: { usage?: ContextUsage | null }) {
  const used = usage?.used_tokens;
  const window = usage?.window_tokens;
  if (!used || used <= 0 || !window || window <= 0) return null;

  const percent = Math.round((used / window) * 100);
  const percentLabel = percent <= 0 ? "<1%" : `${percent}%`;
  const isWarning = percent >= WARNING_THRESHOLD;
  const usedLabel = formatTokens(used);
  const windowLabel = formatTokens(window);
  const budget = usage?.budget_tokens;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          aria-label={`Context window: ${usedLabel} of ${windowLabel} tokens used (${percentLabel})`}
          className={`shrink-0 whitespace-nowrap pb-3 typo-caption-sm ${
            isWarning
              ? "text-onSurface-danger-primary"
              : "text-onSurface-default-tertiary"
          }`}
        >
          {usedLabel} / {windowLabel} · {percentLabel}
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-xs">
        <p>Context used: {used.toLocaleString()} tokens (last model call)</p>
        <p>Context window: {window.toLocaleString()} tokens</p>
        <p>
          Compression budget:{" "}
          {budget ? `${budget.toLocaleString()} tokens` : "disabled"}
        </p>
        {isWarning ? <p>Approaching the context window limit.</p> : null}
      </TooltipContent>
    </Tooltip>
  );
}

export function ChatComposer({
  onSend,
  onStop,
  isStreaming,
  cancelRequested,
  contextUsage,
}: ChatComposerProps) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  useEffect(() => {
    if (!isStreaming) textareaRef.current?.focus();
  }, [isStreaming]);

  const submit = async () => {
    const message = value.trim();
    if (!message || isStreaming) return;
    setValue("");
    const result = await onSend(message);
    if (result && !result.ok && typeof result.draft === "string") {
      // A 409 means the server did not persist this message: give the text
      // back, but never overwrite something the user typed in the meantime.
      setValue((current) =>
        current.length > 0 ? current : (result.draft ?? ""),
      );
    }
  };

  return (
    <div>
      <div className="flex items-end gap-2">
        <Textarea
          ref={textareaRef}
          rows={1}
          value={value}
          placeholder="Message the agent..."
          aria-label="Message the agent"
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void submit();
            }
          }}
          className="flex-1"
          textareaClassName="h-auto min-h-[44px] max-h-[200px] py-2.5"
        />
        <ContextMeter usage={contextUsage} />
        {isStreaming ? (
          <div className="flex items-center gap-2">
            {cancelRequested && (
              <span className="typo-caption-sm text-onSurface-default-tertiary">
                Stopping…
              </span>
            )}
            <Button
              type="button"
              variant="outline"
              size="icon"
              onClick={onStop}
              disabled={cancelRequested}
              aria-label={cancelRequested ? "Stopping" : "Stop generating"}
            >
              {cancelRequested ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Square className="size-4" />
              )}
            </Button>
          </div>
        ) : (
          <Button
            type="button"
            size="icon"
            onClick={() => void submit()}
            disabled={!value.trim()}
            aria-label="Send message"
          >
            <ArrowUp className="size-4" />
          </Button>
        )}
      </div>
      <p className="mt-1.5 typo-caption-sm text-onSurface-default-tertiary">
        ENTER TO SEND · SHIFT+ENTER FOR A NEW LINE
      </p>
    </div>
  );
}
