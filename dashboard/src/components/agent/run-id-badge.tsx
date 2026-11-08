"use client";

import { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";
import { CopyToClipboard } from "react-copy-to-clipboard";

import { useI18n } from "@/i18n/provider";

export function RunIdBadge({ runId }: { runId: string }) {
  const [copied, setCopied] = useState(false);
  const { t } = useI18n();

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  return (
    <CopyToClipboard text={runId} onCopy={() => setCopied(true)}>
      <button
        type="button"
        aria-label={copied ? t("agent.runId.copied") : t("agent.runId.copy")}
        title={t("agent.runId.title", { runId })}
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 typo-caption-sm text-onSurface-default-tertiary transition-colors hover:bg-surface-default-secondary hover:text-onSurface-default-primary"
      >
        <span>{t("agent.runId.label", { runId: runId.slice(0, 8) })}</span>
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      </button>
    </CopyToClipboard>
  );
}
