"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUp, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

interface ChatComposerProps {
  onSend: (message: string) => void;
  onStop: () => void;
  isStreaming: boolean;
}

const MAX_HEIGHT = 200;

export function ChatComposer({
  onSend,
  onStop,
  isStreaming,
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

  const submit = () => {
    const message = value.trim();
    if (!message || isStreaming) return;
    setValue("");
    onSend(message);
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
              submit();
            }
          }}
          className="flex-1"
          textareaClassName="h-auto min-h-[44px] max-h-[200px] py-2.5"
        />
        {isStreaming ? (
          <Button
            type="button"
            variant="outline"
            size="icon"
            onClick={onStop}
            aria-label="Stop generating"
          >
            <Square className="size-4" />
          </Button>
        ) : (
          <Button
            type="button"
            size="icon"
            onClick={submit}
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
