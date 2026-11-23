"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUp, Loader2, Paperclip, Square, X } from "lucide-react";

import { AuthenticatedImage } from "@/components/agent/authenticated-image";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { formatTokens } from "@/lib/utils";
import { useI18n } from "@/i18n/provider";
import { uploadAttachment } from "@/utils/api";
import type { SendResult } from "@/hooks/use-agent-chat";
import type { AgentCapabilities, Attachment, ContextUsage } from "@/types/api";

interface PendingAttachment {
  key: string;
  file: File;
  status: "uploading" | "ready" | "error";
  progress: number;
  attachment?: Attachment;
  error?: string;
}

interface ChatComposerProps {
  onSend: (
    message: string,
    attachments: Attachment[],
  ) => Promise<SendResult> | void;
  onStop: () => void;
  isStreaming: boolean;
  cancelRequested: boolean;
  contextUsage?: ContextUsage | null;
  capabilities?: AgentCapabilities | null;
}

const MAX_HEIGHT = 200;
const WARNING_THRESHOLD = 90;
const DEFAULT_MAX_IMAGES = 4;
const DEFAULT_MAX_BYTES = 10 * 1024 * 1024;
const DEFAULT_MIME_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/gif",
];

function ContextMeter({ usage }: { usage?: ContextUsage | null }) {
  const { t, formatNumber } = useI18n();
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
          aria-label={t("agent.composer.contextWindowAria", {
            used: usedLabel,
            total: windowLabel,
            percent: percentLabel,
          })}
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
        <p>{t("agent.composer.contextUsed", { used: formatNumber(used) })}</p>
        <p>
          {t("agent.composer.contextWindow", {
            total: formatNumber(window),
          })}
        </p>
        <p>
          {budget
            ? t("agent.composer.compressionBudget", {
                budget: formatNumber(budget),
              })
            : t("agent.composer.compressionDisabled")}
        </p>
        {isWarning ? <p>{t("agent.composer.approachingLimit")}</p> : null}
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
  capabilities,
}: ChatComposerProps) {
  const { t } = useI18n();
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canAttach = Boolean(capabilities?.image_input);
  const maxImages = capabilities?.max_images ?? DEFAULT_MAX_IMAGES;
  const maxBytes = capabilities?.max_bytes ?? DEFAULT_MAX_BYTES;
  const allowedMimeTypes =
    capabilities?.allowed_mime_types ?? DEFAULT_MIME_TYPES;
  const isUploading = attachments.some((item) => item.status === "uploading");
  const readyAttachments = attachments
    .filter((item) => item.status === "ready" && item.attachment)
    .map((item) => item.attachment as Attachment);
  const canSend =
    !isStreaming &&
    !isUploading &&
    (value.trim().length > 0 || readyAttachments.length > 0);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT)}px`;
  }, [value]);

  useEffect(() => {
    if (!isStreaming) textareaRef.current?.focus();
  }, [isStreaming]);

  const uploadFiles = (files: File[]) => {
    if (!canAttach || files.length === 0) return;
    const currentCount = attachments.filter(
      (item) => item.status !== "error",
    ).length;
    const room = Math.max(0, maxImages - currentCount);
    if (room === 0) {
      toast({
        title: t("agent.composer.tooManyImages", { max: maxImages }),
        variant: "destructive",
      });
      return;
    }
    const accepted = files.slice(0, room);
    if (files.length > room) {
      toast({
        title: t("agent.composer.tooManyImages", { max: maxImages }),
        variant: "destructive",
      });
    }

    const stamp = Date.now();
    const pending: PendingAttachment[] = [];
    accepted.forEach((file, index) => {
      if (!allowedMimeTypes.includes(file.type)) {
        toast({
          title: t("agent.composer.unsupportedImage", { name: file.name }),
          variant: "destructive",
        });
        return;
      }
      if (file.size > maxBytes) {
        toast({
          title: t("agent.composer.imageTooLarge", { name: file.name }),
          variant: "destructive",
        });
        return;
      }
      const key = `${stamp}-${index}-${file.name}`;
      pending.push({
        key,
        file,
        status: "uploading",
        progress: 0,
      });
    });
    if (pending.length === 0) return;
    setAttachments((previous) => [...previous, ...pending]);

    for (const item of pending) {
      void uploadAttachment(item.file, {
        onProgress: (progress) => {
          setAttachments((previous) =>
            previous.map((entry) =>
              entry.key === item.key ? { ...entry, progress } : entry,
            ),
          );
        },
      })
        .then((attachment) => {
          setAttachments((previous) =>
            previous.map((entry) =>
              entry.key === item.key
                ? { ...entry, status: "ready", progress: 100, attachment }
                : entry,
            ),
          );
        })
        .catch(() => {
          setAttachments((previous) =>
            previous.map((entry) =>
              entry.key === item.key
                ? { ...entry, status: "error", error: "upload_failed" }
                : entry,
            ),
          );
          toast({
            title: t("agent.composer.uploadFailed", { name: item.file.name }),
            variant: "destructive",
          });
        });
    }
  };

  const removeAttachment = (key: string) => {
    setAttachments((previous) => previous.filter((item) => item.key !== key));
  };

  const submit = async () => {
    const message = value.trim();
    if (!canSend || isStreaming) return;
    const sent = readyAttachments;
    setValue("");
    setAttachments([]);
    const result = await onSend(message, sent);
    if (result && !result.ok) {
      setValue((current) =>
        current.length > 0 ? current : (result.draft ?? ""),
      );
      setAttachments((previous) =>
        previous.length > 0
          ? previous
          : sent.map((attachment, index) => ({
              key: `restored-${Date.now()}-${index}`,
              file: new File([], attachment.name),
              status: "ready" as const,
              progress: 100,
              attachment,
            })),
      );
    }
  };

  const handleFiles = (files: FileList | null) => {
    if (!files) return;
    uploadFiles(Array.from(files));
  };

  return (
    <div
      onDragOver={(event) => {
        if (!canAttach) return;
        event.preventDefault();
        setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={(event) => {
        if (!canAttach) return;
        event.preventDefault();
        setIsDragging(false);
        handleFiles(event.dataTransfer.files);
      }}
      className={
        isDragging
          ? "rounded-xl ring-2 ring-memBorderPrimary ring-offset-2 ring-offset-surface-default-primary"
          : undefined
      }
    >
      {attachments.length > 0 ? (
        <div className="mb-2 flex flex-wrap gap-2">
          {attachments.map((item) => (
            <div
              key={item.key}
              className={`relative flex h-16 w-16 items-center justify-center overflow-hidden rounded-lg border ${
                item.status === "error"
                  ? "border-onSurface-danger-primary"
                  : "border-memBorderPrimary"
              } bg-surface-default-secondary`}
              title={item.file.name}
            >
              {item.status === "ready" && item.attachment ? (
                <AuthenticatedImage
                  attachmentId={item.attachment.id}
                  alt={t("agent.composer.imageAlt", { index: 1 })}
                  className="h-full w-full max-h-16 min-h-0 rounded-none border-0 object-cover"
                />
              ) : (
                <div className="flex flex-col items-center gap-1 px-1 text-center">
                  <span className="line-clamp-2 break-all typo-caption-sm text-onSurface-default-tertiary">
                    {item.file.name}
                  </span>
                  {item.status === "uploading" ? (
                    <span className="typo-caption-sm text-onSurface-default-tertiary">
                      {item.progress > 0
                        ? `${item.progress}%`
                        : t("agent.composer.uploading")}
                    </span>
                  ) : (
                    <span className="typo-caption-sm text-onSurface-danger-primary">
                      {t("agent.composer.uploadFailed", {
                        name: item.file.name,
                      })}
                    </span>
                  )}
                </div>
              )}
              <button
                type="button"
                onClick={() => removeAttachment(item.key)}
                aria-label={t("agent.composer.removeAttachment", {
                  name: item.file.name,
                })}
                className="absolute right-0.5 top-0.5 rounded-full bg-surface-default-primary/90 p-0.5 text-onSurface-default-secondary hover:text-onSurface-default-primary"
              >
                <X className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      ) : null}

      <div className="flex items-end gap-2">
        <input
          ref={fileInputRef}
          type="file"
          accept={allowedMimeTypes.join(",")}
          multiple
          className="hidden"
          onChange={(event) => {
            handleFiles(event.target.files);
            event.target.value = "";
          }}
        />
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => fileInputRef.current?.click()}
              disabled={!canAttach || isStreaming}
              aria-label={
                canAttach
                  ? t("agent.composer.attachAria")
                  : t("agent.composer.imageInputUnavailable")
              }
            >
              <Paperclip className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top">
            {canAttach
              ? t("agent.composer.attach")
              : t("agent.composer.imageInputUnavailable")}
          </TooltipContent>
        </Tooltip>
        <Textarea
          ref={textareaRef}
          rows={1}
          value={value}
          placeholder={t("agent.composer.placeholder")}
          aria-label={t("agent.composer.aria")}
          onChange={(event) => setValue(event.target.value)}
          onPaste={(event) => {
            if (!canAttach) return;
            const files = Array.from(event.clipboardData.files);
            if (files.length > 0) {
              event.preventDefault();
              uploadFiles(files);
            }
          }}
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
                {t("agent.composer.stopping")}
              </span>
            )}
            <Button
              type="button"
              variant="outline"
              size="icon"
              onClick={onStop}
              disabled={cancelRequested}
              aria-label={
                cancelRequested
                  ? t("agent.composer.stoppingAria")
                  : t("agent.composer.stop")
              }
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
            disabled={!canSend}
            aria-label={t("agent.composer.send")}
          >
            <ArrowUp className="size-4" />
          </Button>
        )}
      </div>
      <p className="mt-1.5 typo-caption-sm text-onSurface-default-tertiary">
        {t("agent.composer.hint")}
      </p>
    </div>
  );
}
