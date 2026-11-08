"use client";

import { useState } from "react";
import { Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { toast } from "@/components/ui/use-toast";
import { api } from "@/utils/api";
import { ENTITY_ENDPOINTS } from "@/utils/api-endpoints";
import { getErrorMessage } from "@/lib/error-message";
import { useApiQuery } from "@/hooks/use-api-query";
import type { MessageKey } from "@/i18n/messages/en";
import { useI18n } from "@/i18n/provider";
import { Entity } from "@/types/api";

const ENTITY_TYPE_KEYS: Record<Entity["type"], MessageKey> = {
  user: "enums.entityType.user",
  agent: "enums.entityType.agent",
  run: "enums.entityType.run",
};

export default function EntitiesPage() {
  const [entityToDelete, setEntityToDelete] = useState<Entity | null>(null);
  const { t, formatDate } = useI18n();

  const {
    data: entities = [],
    isLoading,
    refetch,
  } = useApiQuery<Entity[]>(
    async () => {
      const res = await api.get<Entity[]>(ENTITY_ENDPOINTS.BASE);
      return res.data ?? [];
    },
    { errorToast: t("entities.failedToLoad"), initialData: [] },
  );

  const handleDelete = async () => {
    if (!entityToDelete) return;
    try {
      await api.delete(
        ENTITY_ENDPOINTS.BY_ID(entityToDelete.type, entityToDelete.id),
      );
      toast({ title: t("entities.deleted"), variant: "success" });
      setEntityToDelete(null);
      void refetch();
    } catch (error) {
      toast({
        title: t("entities.deleteFailed"),
        description: getErrorMessage(error, t("common.error.generic")),
        variant: "destructive",
      });
    }
  };

  const columns = [
    {
      key: "type" as keyof Entity,
      label: t("entities.table.type"),
      width: 100,
      render: (value: Entity["type"]) => (
        <Badge variant="outline">{t(ENTITY_TYPE_KEYS[value])}</Badge>
      ),
    },
    {
      key: "id" as keyof Entity,
      label: t("entities.table.id"),
      width: 280,
      render: (value: string) => (
        <span className="font-mono text-sm truncate">{value}</span>
      ),
    },
    {
      key: "total_memories" as keyof Entity,
      label: t("entities.table.memories"),
      width: 100,
      align: "right" as const,
    },
    {
      key: "updated_at" as keyof Entity,
      label: t("entities.table.lastActive"),
      width: 140,
      render: (value: string | null) => (value ? formatDate(value) : "--"),
    },
    {
      key: "id" as keyof Entity,
      label: "",
      width: 40,
      render: (_: string, row: Entity) => (
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setEntityToDelete(row)}
          className="size-7"
        >
          <Trash2 className="size-3.5 text-onSurface-danger-primary" />
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold font-fustat">
        {t("entities.title")}
      </h1>

      {isLoading ? (
        <TableSkeleton rows={5} columns={5} />
      ) : entities.length === 0 ? (
        <EmptyState
          title={t("entities.empty.title")}
          description={t("entities.empty.description")}
        />
      ) : (
        <Card className="border-memBorder-primary overflow-hidden">
          <DataTable
            data={entities}
            columns={columns}
            getRowKey={(row) => `${row.type}:${row.id}`}
          />
        </Card>
      )}

      <DeleteConfirmationModal
        isOpen={!!entityToDelete}
        onClose={() => setEntityToDelete(null)}
        onConfirm={handleDelete}
        title={t("entities.delete.title")}
        description={t("entities.delete.description")}
        itemName={entityToDelete?.id ?? ""}
        confirmButtonText={t("common.delete")}
      />
    </div>
  );
}
