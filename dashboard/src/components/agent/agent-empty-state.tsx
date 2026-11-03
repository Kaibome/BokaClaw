import { Bot } from "lucide-react";

import { cn } from "@/lib/utils";

interface AgentEmptyStateProps {
  title: string;
  description: string;
  className?: string;
}

export function AgentEmptyState({
  title,
  description,
  className,
}: AgentEmptyStateProps) {
  return (
    <div
      className={cn(
        "flex h-full flex-col items-center justify-center gap-3 px-6 text-center",
        className,
      )}
    >
      <div className="flex size-10 items-center justify-center rounded-full border border-memBorder-primary bg-surface-default-secondary">
        <Bot className="size-5 text-onSurface-default-secondary" />
      </div>
      <div className="space-y-1">
        <p className="typo-body-sm text-onSurface-default-primary">{title}</p>
        <p className="max-w-sm text-sm text-onSurface-default-tertiary">
          {description}
        </p>
      </div>
    </div>
  );
}
