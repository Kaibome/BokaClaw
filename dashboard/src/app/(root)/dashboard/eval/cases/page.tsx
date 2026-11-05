"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { formatDistanceToNow } from "date-fns";
import { ArrowLeft, Pencil, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/use-toast";
import { DataTable } from "@/components/shared/data-table";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { getErrorMessage } from "@/lib/error-message";
import { useApiQuery } from "@/hooks/use-api-query";
import { api } from "@/utils/api";
import { EVAL_ENDPOINTS } from "@/utils/api-endpoints";
import {
  EvalGolden,
  EvalGoldenDetail,
  EvalGoldenUpsert,
  EvalToolCall,
} from "@/types/api";

type FormState = {
  eval_case_id: string;
  input: string;
  expected_output: string;
  expected_tools: string;
  context: string;
  notes: string;
};

const EMPTY_FORM: FormState = {
  eval_case_id: "",
  input: "",
  expected_output: "",
  expected_tools: "[]",
  context: "",
  notes: "",
};

function parseTools(raw: string): EvalToolCall[] {
  const value = raw.trim();
  if (!value) return [];
  const parsed: unknown = JSON.parse(value);
  if (!Array.isArray(parsed)) {
    throw new Error("Expected tools must be a JSON array.");
  }
  return parsed.map((item, index) => {
    if (
      !item ||
      typeof item !== "object" ||
      typeof (item as { name?: unknown }).name !== "string" ||
      !(item as { name: string }).name.trim()
    ) {
      throw new Error(`expected_tools[${index}] needs a non-empty "name".`);
    }
    return item as EvalToolCall;
  });
}

export default function EvalCasesPage() {
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<EvalGoldenDetail | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<EvalGolden | null>(null);
  const [removing, setRemoving] = useState(false);

  const {
    data: cases = [],
    isLoading,
    refetch,
  } = useApiQuery<EvalGolden[]>(
    async () => {
      const res = await api.get<EvalGolden[]>(EVAL_ENDPOINTS.GOLDENS);
      return res.data ?? [];
    },
    { errorToast: "Failed to load eval cases", initialData: [] },
  );

  const update = (key: keyof FormState, value: string) =>
    setForm((current) => ({ ...current, [key]: value }));

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormOpen(true);
  };

  const openEdit = async (evalCaseId: string) => {
    try {
      const res = await api.get<EvalGoldenDetail>(
        EVAL_ENDPOINTS.GOLDEN_BY_ID(evalCaseId),
      );
      setEditing(res.data);
      setForm({
        eval_case_id: res.data.eval_case_id,
        input: res.data.input,
        expected_output: res.data.expected_output ?? "",
        expected_tools: JSON.stringify(res.data.expected_tools ?? [], null, 2),
        context: (res.data.context ?? []).join("\n"),
        notes: res.data.notes ?? "",
      });
      setFormOpen(true);
    } catch (err) {
      toast({
        title: "Failed to load case",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const evalCaseId = form.eval_case_id.trim();
    if (!evalCaseId || !form.input.trim()) {
      toast({
        title: "Case ID and input are required",
        variant: "destructive",
      });
      return;
    }
    let tools: EvalToolCall[];
    try {
      tools = parseTools(form.expected_tools);
    } catch (err) {
      toast({
        title: "Invalid expected tools JSON",
        description: getErrorMessage(err),
        variant: "destructive",
      });
      return;
    }
    const body: EvalGoldenUpsert = {
      input: form.input.trim(),
      expected_output: form.expected_output.trim() || null,
      expected_tools: tools,
      context: form.context
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
      notes: form.notes.trim() || null,
    };
    setSaving(true);
    try {
      if (editing) {
        await api.put(EVAL_ENDPOINTS.GOLDEN_BY_ID(editing.eval_case_id), body);
      } else {
        await api.post(EVAL_ENDPOINTS.GOLDENS, {
          eval_case_id: evalCaseId,
          ...body,
        });
      }
      toast({
        title: editing ? "Case updated" : "Case created",
        description: evalCaseId,
        variant: "success",
      });
      setFormOpen(false);
      setEditing(null);
      await refetch();
    } catch (err) {
      toast({
        title: editing ? "Failed to update case" : "Failed to create case",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setRemoving(true);
    try {
      await api.delete(EVAL_ENDPOINTS.GOLDEN_BY_ID(deleting.eval_case_id));
      toast({
        title: "Case deleted",
        description: deleting.eval_case_id,
        variant: "success",
      });
      setDeleting(null);
      await refetch();
    } catch (err) {
      toast({
        title: "Failed to delete case",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    } finally {
      setRemoving(false);
    }
  };

  const columns = [
    {
      key: "eval_case_id" as keyof EvalGolden,
      label: "Case",
      width: 220,
      render: (value: string) => (
        <span className="font-mono text-xs break-all">{value}</span>
      ),
    },
    {
      key: "input_preview" as keyof EvalGolden,
      label: "Input",
      width: 320,
      render: (value: string) => (
        <span className="line-clamp-2 text-xs text-onSurface-default-secondary">
          {value}
        </span>
      ),
    },
    {
      key: "expected_tools" as keyof EvalGolden,
      label: "Tools",
      width: 160,
      render: (value: string[]) =>
        value.length > 0 ? (
          <span className="font-mono text-xs">{value.join(", ")}</span>
        ) : (
          <span className="text-xs text-onSurface-default-tertiary">—</span>
        ),
    },
    {
      key: "updated_at" as keyof EvalGolden,
      label: "Updated",
      width: 120,
      render: (value: string) => (
        <span className="text-xs text-onSurface-default-tertiary">
          {formatDistanceToNow(new Date(value), { addSuffix: true })}
        </span>
      ),
    },
    {
      key: "eval_case_id" as keyof EvalGolden,
      label: "",
      width: 110,
      align: "right" as const,
      render: (_value: string, row: EvalGolden) => (
        <div className="flex justify-end gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Edit ${row.eval_case_id}`}
            onClick={() => void openEdit(row.eval_case_id)}
          >
            <Pencil className="size-4" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Delete ${row.eval_case_id}`}
            onClick={() => setDeleting(row)}
          >
            <Trash2 className="size-4 text-onSurface-danger-primary" />
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-1">
          <Link
            href="/dashboard/eval"
            className="inline-flex items-center gap-1 text-xs text-onSurface-default-tertiary hover:underline"
          >
            <ArrowLeft className="size-3.5" />
            Back to Eval
          </Link>
          <h1 className="text-xl font-semibold font-fustat">Eval cases</h1>
          <p className="text-sm text-onSurface-default-secondary">
            Reference questions, expected answers and expected tools used to
            score agent runs.
          </p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="mr-2 size-4" />
          New case
        </Button>
      </div>

      <Card className="overflow-hidden border-memBorder-primary">
        {cases.length === 0 && !isLoading ? (
          <EmptyState
            title="No eval cases yet"
            description="Create one here, or seed them with scripts/seed_eval_goldens.py."
            image="requests"
          />
        ) : (
          <DataTable
            data={cases}
            columns={columns}
            getRowKey={(row) => row.eval_case_id}
          />
        )}
      </Card>

      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              {editing ? "Edit eval case" : "New eval case"}
            </DialogTitle>
            <DialogDescription>
              Expected tools and context are optional. Cases are frozen into a
              snapshot when an eval is queued, so edits do not rewrite history.
            </DialogDescription>
          </DialogHeader>
          <form className="space-y-4" onSubmit={submit}>
            <div className="space-y-1">
              <Label htmlFor="case-id">Case ID</Label>
              <Input
                id="case-id"
                value={form.eval_case_id}
                onChange={(event) => update("eval_case_id", event.target.value)}
                placeholder="minimax-pricing-golden-02"
                disabled={Boolean(editing)}
                required
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="case-input">Input</Label>
              <Textarea
                id="case-input"
                value={form.input}
                onChange={(event) => update("input", event.target.value)}
                placeholder="The user question this case evaluates."
                rows={3}
                required
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="case-expected-output">
                Expected output (optional)
              </Label>
              <Textarea
                id="case-expected-output"
                value={form.expected_output}
                onChange={(event) =>
                  update("expected_output", event.target.value)
                }
                rows={3}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="case-tools">Expected tools (JSON array)</Label>
              <Textarea
                id="case-tools"
                value={form.expected_tools}
                onChange={(event) =>
                  update("expected_tools", event.target.value)
                }
                className="font-mono text-xs"
                rows={4}
                placeholder='[{"name": "WebSearch"}, {"name": "WebFetch"}]'
              />
              <p className="text-xs text-onSurface-default-tertiary">
                Tool correctness only runs when both this list and the run
                contain tool calls.
              </p>
            </div>
            <div className="space-y-1">
              <Label htmlFor="case-context">Context (one per line)</Label>
              <Textarea
                id="case-context"
                value={form.context}
                onChange={(event) => update("context", event.target.value)}
                rows={2}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="case-notes">Notes (optional)</Label>
              <Textarea
                id="case-notes"
                value={form.notes}
                onChange={(event) => update("notes", event.target.value)}
                rows={2}
                placeholder="Maintenance notes; not part of the golden hash."
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setFormOpen(false)}
                disabled={saving}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={saving}>
                {saving ? "Saving…" : editing ? "Save changes" : "Create case"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <DeleteConfirmationModal
        isOpen={Boolean(deleting)}
        onClose={() => setDeleting(null)}
        onConfirm={() => void confirmDelete()}
        title="Delete eval case"
        description="Deleting a case does not change past eval results; they keep their frozen snapshot."
        itemName={deleting?.eval_case_id ?? ""}
        confirmButtonText={removing ? "Deleting…" : "Delete"}
      />
    </div>
  );
}
