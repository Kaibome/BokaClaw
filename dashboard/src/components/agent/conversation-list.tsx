"use client";

import { MoreHorizontal, Pencil, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n/provider";
import type { Conversation } from "@/types/api";

interface ConversationListProps {
  conversations: Conversation[];
  activeId: string | null;
  isLoading: boolean;
  onSelect: (conversationId: string) => void;
  onNewChat: () => void;
  onRename: (conversation: Conversation) => void;
  onDelete: (conversation: Conversation) => void;
}

export function ConversationList({
  conversations,
  activeId,
  isLoading,
  onSelect,
  onNewChat,
  onRename,
  onDelete,
}: ConversationListProps) {
  const { t, formatRelativeTime } = useI18n();

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-memBorder-primary px-3 py-2.5">
        <span className="typo-caption text-onSurface-default-tertiary">
          {t("agent.conversations")}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-6"
          onClick={onNewChat}
          aria-label={t("agent.newChat")}
        >
          <Plus className="size-3.5" />
        </Button>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-0.5 p-2">
          {isLoading ? (
            <>
              <Skeleton className="h-11 w-full" />
              <Skeleton className="h-11 w-4/5" />
              <Skeleton className="h-11 w-3/5" />
            </>
          ) : conversations.length === 0 ? (
            <p className="px-2 py-6 text-center text-sm text-onSurface-default-tertiary">
              {t("agent.noConversations")}
            </p>
          ) : (
            conversations.map((conversation) => {
              const active = conversation.id === activeId;
              return (
                <div key={conversation.id} className="group relative">
                  <button
                    type="button"
                    onClick={() => onSelect(conversation.id)}
                    className={cn(
                      "flex w-full flex-col items-start gap-0.5 rounded-lg py-2 pl-2.5 pr-8 text-left transition-colors",
                      active
                        ? "bg-surface-default-tertiary"
                        : "hover:bg-surface-default-secondary",
                    )}
                  >
                    <span className="w-full truncate text-sm text-onSurface-default-primary">
                      {conversation.title}
                    </span>
                    <span className="typo-caption-sm text-onSurface-default-tertiary">
                      {formatRelativeTime(conversation.updated_at)}
                    </span>
                  </button>

                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="absolute right-1 top-1.5 size-6 opacity-0 transition-opacity group-hover:opacity-100 data-[state=open]:opacity-100"
                        aria-label={t("agent.conversationActions")}
                      >
                        <MoreHorizontal className="size-3.5" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end">
                      <DropdownMenuItem onClick={() => onRename(conversation)}>
                        <Pencil className="mr-2 size-3.5" />
                        {t("agent.rename")}
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        className="text-onSurface-danger-primary focus:text-onSurface-danger-primary"
                        onClick={() => onDelete(conversation)}
                      >
                        <Trash2 className="mr-2 size-3.5" />
                        {t("agent.delete")}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
              );
            })
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
