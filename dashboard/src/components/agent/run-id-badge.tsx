"use client";

import { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";
import { CopyToClipboard } from "react-copy-to-clipboard";

export function RunIdBadge({ runId }: { runId: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  return (
    <CopyToClipboard text={runId} onCopy={() => setCopied(true)}>
      <button
        type="button"
        aria-label={copied ? "Run id copied" : "Copy run id"}
        title={`Copy run id: ${runId}`}
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 typo-caption-sm text-onSurface-default-tertiary transition-colors hover:bg-surface-default-secondary hover:text-onSurface-default-primary"
      >
        <span>run {runId.slice(0, 8)}</span>
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      </button>
    </CopyToClipboard>
  );
}
