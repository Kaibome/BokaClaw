"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { api } from "@/utils/api";
import { API_KEY_ENDPOINTS } from "@/utils/api-endpoints";
import { toast } from "@/components/ui/use-toast";
import { Plus, Copy, Check, Trash2 } from "lucide-react";
import { CopyToClipboard } from "react-copy-to-clipboard";
import { getErrorMessage } from "@/lib/error-message";
import { useApiQuery } from "@/hooks/use-api-query";
import { useI18n } from "@/i18n/provider";
import { ApiKey, ApiKeyCreateResponse } from "@/types/api";

export default function ApiKeysPage() {
  const { t, formatDate } = useI18n();
  const [createOpen, setCreateOpen] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [newKey, setNewKey] = useState("");
  const [copied, setCopied] = useState(false);
  const [keyToRevoke, setKeyToRevoke] = useState<ApiKey | null>(null);

  const {
    data: keys = [],
    isLoading,
    refetch,
  } = useApiQuery<ApiKey[]>(
    async () => {
      const res = await api.get<ApiKey[]>(API_KEY_ENDPOINTS.BASE);
      return res.data ?? [];
    },
    { errorToast: t("apiKeys.failedToLoad"), initialData: [] },
  );

  const handleCreate = async () => {
    try {
      const res = await api.post<ApiKeyCreateResponse>(API_KEY_ENDPOINTS.BASE, {
        label: newLabel,
      });
      setNewKey(res.data.key);
      void refetch();
    } catch (error) {
      toast({
        title: t("apiKeys.failedToCreate"),
        description: getErrorMessage(error, t("common.error.generic")),
        variant: "destructive",
      });
    }
  };

  const handleRevoke = async () => {
    if (!keyToRevoke) return;
    try {
      await api.delete(API_KEY_ENDPOINTS.BY_ID(keyToRevoke.id));
      toast({ title: t("apiKeys.revoked"), variant: "success" });
      setKeyToRevoke(null);
      void refetch();
    } catch (error) {
      toast({
        title: t("apiKeys.failedToRevoke"),
        description: getErrorMessage(error, t("common.error.generic")),
        variant: "destructive",
      });
    }
  };

  const handleDialogClose = (open: boolean) => {
    if (!open) {
      setNewKey("");
      setNewLabel("");
      setCopied(false);
    }
    setCreateOpen(open);
  };

  const columns = [
    {
      key: "label" as keyof ApiKey,
      label: t("apiKeys.table.label"),
      width: 150,
    },
    {
      key: "key_prefix" as keyof ApiKey,
      label: t("apiKeys.table.key"),
      width: 120,
      render: (value: string) => (
        <code className="text-xs font-mono">{value}...</code>
      ),
    },
    {
      key: "created_at" as keyof ApiKey,
      label: t("apiKeys.table.created"),
      width: 120,
      render: (value: string) => formatDate(value),
    },
    {
      key: "last_used_at" as keyof ApiKey,
      label: t("apiKeys.table.lastUsed"),
      width: 120,
      render: (value: string | null) =>
        value ? formatDate(value) : t("common.never"),
    },
    {
      key: "id" as keyof ApiKey,
      label: "",
      width: 40,
      render: (_: string, row: ApiKey) => (
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setKeyToRevoke(row)}
          className="size-7"
        >
          <Trash2 className="size-3.5 text-onSurface-danger-primary" />
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold font-fustat">
          {t("apiKeys.title")}
        </h1>
        <Dialog open={createOpen} onOpenChange={handleDialogClose}>
          <DialogTrigger asChild>
            <Button size="sm">
              <Plus className="size-4 mr-1" /> {t("apiKeys.create")}
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>{t("apiKeys.createTitle")}</DialogTitle>
            </DialogHeader>
            {!newKey ? (
              <div className="space-y-4 mt-2">
                <div className="space-y-2">
                  <Label htmlFor="api-key-label">{t("apiKeys.label")}</Label>
                  <Input
                    id="api-key-label"
                    value={newLabel}
                    onChange={(e) => setNewLabel(e.target.value)}
                    placeholder={t("apiKeys.labelPlaceholder")}
                  />
                </div>
                <Button
                  onClick={handleCreate}
                  disabled={!newLabel}
                  className="w-full"
                >
                  {t("apiKeys.createButton")}
                </Button>
              </div>
            ) : (
              <div className="space-y-4 mt-2">
                <div className="space-y-2">
                  <Label htmlFor="api-key-new">{t("apiKeys.yourKey")}</Label>
                  <div className="flex gap-2">
                    <Input
                      id="api-key-new"
                      value={newKey}
                      readOnly
                      className="font-mono text-sm"
                    />
                    <CopyToClipboard
                      text={newKey}
                      onCopy={() => {
                        setCopied(true);
                        setTimeout(() => setCopied(false), 2000);
                      }}
                    >
                      <Button
                        variant="outline"
                        size="icon"
                        aria-label={
                          copied ? t("common.copied") : t("common.copy")
                        }
                      >
                        {copied ? (
                          <Check className="size-4" />
                        ) : (
                          <Copy className="size-4" />
                        )}
                      </Button>
                    </CopyToClipboard>
                  </div>
                  <p className="text-xs text-onSurface-danger-primary">
                    {t("apiKeys.saveWarning")}
                  </p>
                </div>
                <Button
                  onClick={() => handleDialogClose(false)}
                  className="w-full"
                >
                  {t("apiKeys.done")}
                </Button>
              </div>
            )}
          </DialogContent>
        </Dialog>
      </div>

      {isLoading ? (
        <TableSkeleton rows={3} columns={4} />
      ) : keys.length === 0 ? (
        <EmptyState
          title={t("apiKeys.empty.title")}
          description={t("apiKeys.empty.description")}
        />
      ) : (
        <Card className="border-memBorder-primary overflow-hidden">
          <DataTable
            data={keys}
            columns={columns}
            getRowKey={(row) => row.id}
          />
        </Card>
      )}

      <DeleteConfirmationModal
        isOpen={!!keyToRevoke}
        onClose={() => setKeyToRevoke(null)}
        onConfirm={handleRevoke}
        title={t("apiKeys.revoke.title")}
        description={t("apiKeys.revoke.description")}
        itemName={keyToRevoke?.label ?? ""}
        confirmButtonText={t("apiKeys.revoke.button")}
      />
    </div>
  );
}
