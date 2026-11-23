"use client";

/* eslint-disable @next/next/no-img-element */

import { useEffect, useState } from "react";

import { Spinner } from "@/components/misc/spinner";
import { cn } from "@/lib/utils";
import { getAttachmentBlob } from "@/utils/api";

interface AuthenticatedImageProps {
  attachmentId: string;
  alt: string;
  className?: string;
  onClick?: () => void;
}

export function AuthenticatedImage({
  attachmentId,
  alt,
  className,
  onClick,
}: AuthenticatedImageProps) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    setFailed(false);

    getAttachmentBlob(attachmentId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachmentId]);

  if (failed) {
    return (
      <div
        className={cn(
          "flex min-h-16 items-center justify-center rounded-lg border border-memBorderPrimary bg-surface-default-secondary px-2 py-3 text-center typo-caption-sm text-onSurface-default-tertiary",
          className,
        )}
      >
        {alt}
      </div>
    );
  }

  if (!url) {
    return (
      <div
        className={cn(
          "flex min-h-16 items-center justify-center rounded-lg border border-memBorderPrimary bg-surface-default-secondary",
          className,
        )}
      >
        <Spinner />
      </div>
    );
  }

  return (
    <img
      src={url}
      alt={alt}
      onClick={onClick}
      className={cn(
        "max-h-64 min-h-16 w-full cursor-zoom-in rounded-lg border border-memBorderPrimary object-cover",
        className,
      )}
    />
  );
}
