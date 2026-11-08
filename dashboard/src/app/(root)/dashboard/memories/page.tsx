"use client";

import { useState } from "react";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card } from "@/components/ui/card";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { toast } from "@/components/ui/use-toast";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { MEMORY_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import { useI18n } from "@/i18n/provider";
import { Memory } from "@/types/api";

const PAGE_SIZE = 20;
// Keep in sync with ALL_MEMORIES_LIMIT in server/main.py.
const MEMORY_FETCH_LIMIT = 1000;

export default function MemoriesPage() {
  const { t, formatDate, formatDateTime } = useI18n();
  const [userId, setUserId] = useState("");
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [memoryToDelete, setMemoryToDelete] = useState<Memory | null>(null);
  const [page, setPage] = useState(0);
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "";

  const {
    data: memories = [],
    isLoading,
    refetch,
  } = useApiQuery<Memory[]>(
    async () => {
      const params = userId.trim()
        ? { user_id: userId.trim(), top_k: MEMORY_FETCH_LIMIT }
        : { top_k: MEMORY_FETCH_LIMIT };
      const res = await api.get(MEMORY_ENDPOINTS.BASE, { params });
      const raw = res.data?.results ?? res.data ?? [];
      return Array.isArray(raw) ? raw : [];
    },
    { errorToast: t("memories.failedToLoad"), initialData: [] },
  );

  const totalPages = Math.ceil(memories.length / PAGE_SIZE);
  const paginatedMemories = memories.slice(
    page * PAGE_SIZE,
    (page + 1) * PAGE_SIZE,
  );

  const handleDelete = async () => {
    if (!memoryToDelete) return;
    try {
      await api.delete(MEMORY_ENDPOINTS.BY_ID(memoryToDelete.id));
      toast({ title: t("memories.deleted"), variant: "success" });
      if (selectedMemory?.id === memoryToDelete.id) setSelectedMemory(null);
      setMemoryToDelete(null);
      void refetch();
    } catch (error) {
      toast({
        title: t("memories.deleteFailed"),
        description: getErrorMessage(error, t("common.error.generic")),
        variant: "destructive",
      });
    }
  };

  const columns = [
    {
      key: "memory" as keyof Memory,
      label: t("memories.table.content"),
      width: 400,
      render: (value: string) => (
        <span className="line-clamp-2 text-sm">{value}</span>
      ),
    },
    {
      key: "user_id" as keyof Memory,
      label: t("memories.table.user"),
      width: 100,
    },
    {
      key: "agent_id" as keyof Memory,
      label: t("memories.table.agent"),
      width: 100,
    },
    {
      key: "created_at" as keyof Memory,
      label: t("memories.table.created"),
      width: 120,
      render: (value: string) => (value ? formatDate(value) : "--"),
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold font-fustat">
        {t("memories.title")}
      </h1>

      <div className="flex gap-3">
        <Input
          placeholder={t("memories.filterPlaceholder")}
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setPage(0);
              refetch();
            }
          }}
          className="w-64"
        />
      </div>

      {isLoading ? (
        <TableSkeleton rows={5} columns={4} />
      ) : memories.length === 0 ? (
        <EmptyState
          title={t("memories.empty.title")}
          description={t("memories.empty.description")}
        >
          <pre className="text-xs text-left bg-surface-default-secondary p-3 rounded font-mono overflow-x-auto mt-3 max-w-lg">
            {`curl -X POST ${apiUrl}/memories \\
  -H "X-API-Key: <your-key>" \\
  -H "Content-Type: application/json" \\
  -d '{"messages": [{"role": "user", "content": "I like hiking"}], "user_id": "alice"}'`}
          </pre>
          <a
            href="https://docs.bokaclaw.ai/memory/overview"
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-onSurface-default-tertiary underline underline-offset-4 hover:text-onSurface-default-primary mt-2"
          >
            {t("memories.restApiReference")}
          </a>
        </EmptyState>
      ) : (
        <>
          <Card className="border-memBorder-primary overflow-hidden">
            <DataTable
              data={paginatedMemories}
              columns={columns}
              getRowKey={(row) => row.id}
              onRowClick={(row) => setSelectedMemory(row)}
              getRowClassName={(row) =>
                selectedMemory?.id === row.id
                  ? "bg-surface-default-tertiary"
                  : undefined
              }
            />
          </Card>
          {totalPages > 1 && (
            <div className="flex items-center justify-between text-sm text-onSurface-default-tertiary">
              <span>
                {t("memories.range", {
                  start: page * PAGE_SIZE + 1,
                  end: Math.min((page + 1) * PAGE_SIZE, memories.length),
                  total: memories.length,
                })}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  {t("common.previous")}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= totalPages - 1}
                  onClick={() => setPage((p) => p + 1)}
                >
                  {t("common.next")}
                </Button>
              </div>
            </div>
          )}
        </>
      )}

      <Sheet
        open={!!selectedMemory}
        onOpenChange={(open) => {
          if (!open) setSelectedMemory(null);
        }}
      >
        <SheetContent className="flex flex-col sm:max-w-md">
          <SheetHeader className="shrink-0">
            <SheetTitle>{t("memories.sheet.title")}</SheetTitle>
            <SheetDescription className="sr-only">
              {t("memories.sheet.description")}
            </SheetDescription>
          </SheetHeader>
          {selectedMemory && (
            <div className="mt-6 min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
              <div className="space-y-1">
                <Label className="text-xs text-onSurface-default-tertiary">
                  {t("memories.sheet.content")}
                </Label>
                <p className="text-sm break-words whitespace-pre-wrap">
                  {selectedMemory.memory}
                </p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <Label className="text-xs text-onSurface-default-tertiary">
                    {t("memories.sheet.id")}
                  </Label>
                  <p className="text-xs font-mono break-all">
                    {selectedMemory.id}
                  </p>
                </div>
                {selectedMemory.user_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      {t("memories.sheet.user")}
                    </Label>
                    <p className="text-sm">{selectedMemory.user_id}</p>
                  </div>
                )}
                {selectedMemory.agent_id && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      {t("memories.sheet.agent")}
                    </Label>
                    <p className="text-sm">{selectedMemory.agent_id}</p>
                  </div>
                )}
                {selectedMemory.created_at && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      {t("memories.sheet.created")}
                    </Label>
                    <p className="text-sm">
                      {formatDateTime(selectedMemory.created_at)}
                    </p>
                  </div>
                )}
              </div>
              <Button
                variant="outline"
                size="sm"
                className="text-onSurface-danger-primary"
                onClick={() => setMemoryToDelete(selectedMemory)}
              >
                <Trash2 className="size-3.5 mr-1" />
                {t("memories.deleteButton")}
              </Button>
            </div>
          )}
        </SheetContent>
      </Sheet>

      <DeleteConfirmationModal
        isOpen={!!memoryToDelete}
        onClose={() => setMemoryToDelete(null)}
        onConfirm={handleDelete}
        title={t("memories.delete.title")}
        description={t("memories.delete.description")}
        itemName={memoryToDelete?.id ?? ""}
        confirmButtonText={t("common.delete")}
      />
    </div>
  );
}
