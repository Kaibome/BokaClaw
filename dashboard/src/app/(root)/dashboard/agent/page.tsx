"use client";

import { useState } from "react";
import { Menu, Pencil, Plus } from "lucide-react";

import { ChatComposer } from "@/components/agent/chat-composer";
import { ConversationList } from "@/components/agent/conversation-list";
import { MessageThread } from "@/components/agent/message-thread";
import { RenameConversationDialog } from "@/components/agent/rename-conversation-dialog";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useAgentChat, type ChatMessageView } from "@/hooks/use-agent-chat";
import { useI18n } from "@/i18n/provider";
import type { Conversation } from "@/types/api";

export default function AgentPage() {
  const { t } = useI18n();
  const {
    conversations,
    activeId,
    activeConversation,
    messages,
    isLoadingConversations,
    isLoadingMessages,
    isStreaming,
    cancelRequested,
    contextUsage,
    error,
    send,
    stop,
    selectConversation,
    startNewChat,
    renameConversation,
    deleteConversation,
  } = useAgentChat();

  const [mobileOpen, setMobileOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Conversation | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Conversation | null>(null);

  const handleSelect = (conversationId: string) => {
    selectConversation(conversationId);
    setMobileOpen(false);
  };

  const handleNewChat = () => {
    startNewChat();
    setMobileOpen(false);
  };

  const handleRetry = (message: ChatMessageView) => {
    if (isStreaming) return;
    const index = messages.findIndex((item) => item.id === message.id);
    const previousUser = messages
      .slice(0, index < 0 ? messages.length : index)
      .reverse()
      .find((item) => item.role === "user");
    if (previousUser) void send(previousUser.content);
  };

  const list = (
    <ConversationList
      conversations={conversations}
      activeId={activeId}
      isLoading={isLoadingConversations}
      onSelect={handleSelect}
      onNewChat={handleNewChat}
      onRename={setRenameTarget}
      onDelete={setDeleteTarget}
    />
  );

  return (
    <div className="flex h-[calc(100vh-118px)] min-h-0 flex-col gap-4">
      <div className="flex shrink-0 items-center justify-between gap-3">
        <h1 className="text-xl font-semibold font-fustat">
          {t("agent.title")}
        </h1>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="icon"
            className="md:hidden"
            onClick={() => setMobileOpen(true)}
            aria-label={t("agent.openConversations")}
          >
            <Menu className="size-4" />
          </Button>
          <Button size="sm" onClick={handleNewChat}>
            <Plus className="mr-1 size-4" />
            {t("agent.newChat")}
          </Button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 gap-4">
        <Card className="hidden w-[260px] shrink-0 flex-col overflow-hidden md:flex">
          {list}
        </Card>

        <Card className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-memBorder-primary px-4 py-2.5">
            <p className="min-w-0 truncate typo-body-sm text-onSurface-default-primary">
              {activeConversation?.title ?? t("agent.newChat")}
            </p>
            {activeConversation && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-8 shrink-0"
                onClick={() => setRenameTarget(activeConversation)}
                aria-label={t("agent.renameConversation")}
              >
                <Pencil className="size-4" />
              </Button>
            )}
          </div>

          <MessageThread
            conversationId={activeId}
            messages={messages}
            isLoading={isLoadingConversations || isLoadingMessages}
            error={error}
            onRetry={handleRetry}
          />

          <div className="shrink-0 border-t border-memBorder-primary p-3">
            <ChatComposer
              onSend={send}
              onStop={() => void stop()}
              isStreaming={isStreaming}
              cancelRequested={cancelRequested}
              contextUsage={contextUsage}
            />
          </div>
        </Card>
      </div>

      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent
          side="left"
          className="flex w-[280px] flex-col p-0"
          aria-describedby={undefined}
        >
          <SheetHeader className="shrink-0 px-3 py-3">
            <SheetTitle className="typo-body-sm">
              {t("agent.conversations")}
            </SheetTitle>
          </SheetHeader>
          <div className="min-h-0 flex-1">{list}</div>
        </SheetContent>
      </Sheet>

      <RenameConversationDialog
        open={renameTarget !== null}
        initialTitle={renameTarget?.title ?? ""}
        onOpenChange={(open) => {
          if (!open) setRenameTarget(null);
        }}
        onSubmit={(title) => {
          if (renameTarget) void renameConversation(renameTarget.id, title);
        }}
      />

      <DeleteConfirmationModal
        isOpen={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => {
          if (deleteTarget) void deleteConversation(deleteTarget.id);
          setDeleteTarget(null);
        }}
        title={t("agent.deleteConversationTitle")}
        description={t("agent.deleteConversationDescription")}
        itemName={t("common.delete")}
      />
    </div>
  );
}
