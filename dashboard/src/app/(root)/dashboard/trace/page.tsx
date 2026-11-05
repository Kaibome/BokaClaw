"use client";

import { Suspense, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { format, formatDistanceToNow } from "date-fns";
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { toast } from "@/components/ui/use-toast";
import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { getErrorMessage } from "@/lib/error-message";
import { api } from "@/utils/api";
import { TRACE_ENDPOINTS } from "@/utils/api-endpoints";
import { useApiQuery } from "@/hooks/use-api-query";
import {
  AgentRunSpanDetail,
  AgentRunSpanSummary,
  SpanStatus,
  SpanType,
} from "@/types/api";

const TRACE_LIMIT = 500;
const PAGE_SIZE = 20;

type StatusFilter = SpanStatus | "all";

const getTypeClassName = (spanType: SpanType) => {
  switch (spanType) {
    case "agent":
      return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900/40 dark:bg-sky-950/40 dark:text-sky-300";
    case "llm":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "tool":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/40 dark:bg-amber-950/40 dark:text-amber-300";
  }
};

const getStatusClassName = (status: SpanStatus) => {
  switch (status) {
    case "completed":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "failed":
      return "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900/40 dark:bg-rose-950/40 dark:text-rose-300";
    case "running":
      return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900/40 dark:bg-sky-950/40 dark:text-sky-300";
    case "aborted":
    case "interrupted":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/40 dark:bg-amber-950/40 dark:text-amber-300";
  }
};

const formatDuration = (value: number | null) =>
  value === null ? "—" : `${value} ms`;

const JsonBlock = ({
  label,
  value,
}: {
  label: string;
  value: Record<string, unknown>;
}) => (
  <div className="space-y-1">
    <Label className="text-xs text-onSurface-default-tertiary">{label}</Label>
    <pre className="max-h-64 overflow-auto rounded-md bg-surface-default-secondary p-3 text-xs font-mono whitespace-pre-wrap break-all">
      {JSON.stringify(value ?? {}, null, 2)}
    </pre>
  </div>
);

function TracePageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [runId, setRunId] = useState(
    () => searchParams.get("run_id")?.trim() ?? "",
  );
  const [status, setStatus] = useState<StatusFilter>("all");
  const [selectedSpan, setSelectedSpan] = useState<AgentRunSpanDetail | null>(
    null,
  );
  const [detailLoading, setDetailLoading] = useState(false);
  const [childSpans, setChildSpans] = useState<AgentRunSpanSummary[] | null>(
    null,
  );
  const [childrenLoading, setChildrenLoading] = useState(false);
  const [childrenError, setChildrenError] = useState<string | null>(null);
  const [spanHistory, setSpanHistory] = useState<AgentRunSpanDetail[]>([]);
  const detailRequestRef = useRef(0);

  const {
    data: spans = [],
    isLoading,
    error,
    refetch,
  } = useApiQuery<AgentRunSpanSummary[]>(
    async () => {
      const res = await api.get<AgentRunSpanSummary[]>(TRACE_ENDPOINTS.BASE, {
        params: { limit: TRACE_LIMIT, span_type: "agent" },
      });
      setLastUpdated(new Date().toISOString());
      return res.data ?? [];
    },
    { errorToast: "Failed to load traces", initialData: [] },
  );

  const filteredSpans = useMemo(() => {
    const needle = runId.trim().toLowerCase();
    return spans.filter((span) => {
      if (status !== "all" && span.status !== status) return false;
      if (needle && !span.run_id.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [spans, status, runId]);

  const totalSpans = filteredSpans.length;
  const totalRuns = new Set(filteredSpans.map((span) => span.run_id)).size;
  const failedSpans = filteredSpans.filter(
    (span) => span.status === "failed",
  ).length;
  const spansWithDuration = filteredSpans.filter(
    (span) => span.duration_ms !== null,
  );
  const averageDuration =
    spansWithDuration.length > 0
      ? Math.round(
          spansWithDuration.reduce(
            (sum, span) => sum + (span.duration_ms ?? 0),
            0,
          ) / spansWithDuration.length,
        )
      : null;

  const loadSpanDetail = async (row: AgentRunSpanSummary) => {
    const requestId = ++detailRequestRef.current;
    setDetailLoading(true);
    try {
      const res = await api.get<AgentRunSpanDetail>(
        TRACE_ENDPOINTS.BY_ID(row.id),
      );
      if (requestId === detailRequestRef.current) {
        setSelectedSpan(res.data);
      }
    } catch (err) {
      if (requestId === detailRequestRef.current) {
        toast({
          title: "Failed to load span",
          description: getErrorMessage(err),
          variant: "destructive",
        });
      }
    } finally {
      if (requestId === detailRequestRef.current) {
        setDetailLoading(false);
      }
    }
  };

  const openSpan = async (row: AgentRunSpanSummary) => {
    setSpanHistory([]);
    setChildSpans(null);
    setChildrenError(null);
    await loadSpanDetail(row);
  };

  const openChildSpan = async (child: AgentRunSpanSummary) => {
    if (selectedSpan && selectedSpan.id !== child.id) {
      setSpanHistory((previous) => [...previous, selectedSpan]);
    }
    await loadSpanDetail(child);
  };

  const goBack = () => {
    const previous = spanHistory[spanHistory.length - 1];
    if (!previous) return;
    detailRequestRef.current += 1;
    setDetailLoading(false);
    setSelectedSpan(previous);
    setSpanHistory(spanHistory.slice(0, -1));
  };

  const toggleChildSpans = async () => {
    if (!selectedSpan) return;
    if (childSpans !== null) {
      setChildSpans(null);
      return;
    }
    setChildrenLoading(true);
    setChildrenError(null);
    try {
      // The tree is two levels deep: children of a run's agent span are its
      // llm/tool calls. Filter by parent so a nested agent span would not leak.
      const res = await api.get<AgentRunSpanSummary[]>(TRACE_ENDPOINTS.BASE, {
        params: { run_id: selectedSpan.run_id, limit: TRACE_LIMIT },
      });
      const rows = (res.data ?? [])
        .filter((span) => span.parent_span_id === selectedSpan.span_id)
        .sort((a, b) => a.started_at.localeCompare(b.started_at));
      setChildSpans(rows);
    } catch (err) {
      setChildrenError(getErrorMessage(err));
    } finally {
      setChildrenLoading(false);
    }
  };

  const columns = [
    {
      key: "started_at" as keyof AgentRunSpanSummary,
      label: "Time",
      width: 140,
      render: (value: string) => (
        <span title={format(new Date(value), "PPpp")}>
          {formatDistanceToNow(new Date(value), { addSuffix: true })}
        </span>
      ),
    },
    {
      key: "run_id" as keyof AgentRunSpanSummary,
      label: "Run",
      width: 110,
      render: (value: string) => (
        <span className="font-mono text-xs" title={value}>
          {value.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "name" as keyof AgentRunSpanSummary,
      label: "Name",
      width: 240,
      render: (value: string) => (
        <span className="font-mono text-xs break-all">{value}</span>
      ),
    },
    {
      key: "status" as keyof AgentRunSpanSummary,
      label: "Status",
      width: 110,
      render: (value: SpanStatus) => (
        <Badge variant="outline" className={getStatusClassName(value)}>
          {value}
        </Badge>
      ),
    },
    {
      key: "iteration" as keyof AgentRunSpanSummary,
      label: "Iter",
      width: 60,
      align: "right" as const,
      render: (value: number | null) => (value === null ? "—" : value),
    },
    {
      key: "duration_ms" as keyof AgentRunSpanSummary,
      label: "Duration",
      width: 100,
      align: "right" as const,
      render: (value: number | null) => formatDuration(value),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold font-fustat">Trace</h1>
          <p className="text-sm text-onSurface-default-secondary">
            One row per agent run. Open a run to inspect its child spans.
          </p>
          {lastUpdated && (
            <p className="text-xs text-onSurface-default-tertiary">
              Last updated{" "}
              {formatDistanceToNow(new Date(lastUpdated), { addSuffix: true })}
            </p>
          )}
        </div>
        <Button
          variant="outline"
          onClick={() => {
            setPage(0);
            void refetch();
          }}
          disabled={isLoading}
        >
          <RefreshCw className="size-4 mr-2" />
          Refresh
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {[
          { label: "Runs", value: totalRuns },
          {
            label: "Avg Duration",
            value: averageDuration === null ? "--" : `${averageDuration} ms`,
          },
          {
            label: "Errors",
            value:
              totalSpans > 0
                ? `${failedSpans} (${Math.round((failedSpans / totalSpans) * 100)}%)`
                : "--",
          },
        ].map((card) => (
          <Card key={card.label} className="border-memBorder-primary">
            <CardContent className="p-5">
              <p className="text-xs text-onSurface-default-tertiary">
                {card.label}
              </p>
              <p className="mt-1 text-2xl font-semibold">{card.value}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="border-memBorder-primary">
        <CardContent className="flex flex-col gap-3 p-4 md:flex-row md:items-end">
          <div className="w-full space-y-1 md:max-w-xs">
            <Label htmlFor="trace-run">Run ID</Label>
            <Input
              id="trace-run"
              placeholder="Filter by run id"
              value={runId}
              onChange={(event) => {
                setRunId(event.target.value);
                setPage(0);
              }}
            />
          </div>
          <div className="w-full space-y-1 md:w-44">
            <Label>Status</Label>
            <Select
              value={status}
              onValueChange={(value) => {
                setStatus(value as StatusFilter);
                setPage(0);
              }}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All statuses</SelectItem>
                <SelectItem value="running">running</SelectItem>
                <SelectItem value="completed">completed</SelectItem>
                <SelectItem value="failed">failed</SelectItem>
                <SelectItem value="aborted">aborted</SelectItem>
                <SelectItem value="interrupted">interrupted</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {error && (
        <Card className="border-memBorder-primary">
          <CardContent className="p-4 text-sm text-onSurface-danger-primary">
            {error}
          </CardContent>
        </Card>
      )}

      {isLoading ? (
        <TableSkeleton rows={6} columns={6} />
      ) : filteredSpans.length === 0 ? (
        <EmptyState
          title="No runs yet"
          description="Agent runs will appear here once one starts."
          image="requests"
        />
      ) : (
        <>
          <Card className="border-memBorder-primary overflow-hidden">
            <DataTable
              data={filteredSpans.slice(
                page * PAGE_SIZE,
                (page + 1) * PAGE_SIZE,
              )}
              columns={columns}
              getRowKey={(row) => row.id}
              onRowClick={(row) => void openSpan(row)}
            />
          </Card>
          {filteredSpans.length > PAGE_SIZE && (
            <div className="flex items-center justify-between text-sm text-onSurface-default-tertiary">
              <span>
                {page * PAGE_SIZE + 1}–
                {Math.min((page + 1) * PAGE_SIZE, filteredSpans.length)} of{" "}
                {filteredSpans.length}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={(page + 1) * PAGE_SIZE >= filteredSpans.length}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </>
      )}

      <Sheet
        open={!!selectedSpan}
        onOpenChange={(open) => {
          if (!open) {
            detailRequestRef.current += 1;
            setSelectedSpan(null);
            setSpanHistory([]);
            setChildSpans(null);
            setChildrenError(null);
          }
        }}
      >
        <SheetContent className="flex flex-col sm:max-w-lg">
          <SheetHeader className="shrink-0">
            <SheetTitle>Span Detail</SheetTitle>
            <SheetDescription className="sr-only">
              View span timing, payloads and attributes
            </SheetDescription>
          </SheetHeader>
          {spanHistory.length > 0 && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="-ml-2 mt-2 w-fit gap-1.5"
              onClick={goBack}
            >
              <ArrowLeft className="size-4" />
              Back to {spanHistory[spanHistory.length - 1].span_id}
            </Button>
          )}
          {detailLoading ? (
            <p className="mt-6 text-sm text-onSurface-default-secondary">
              Loading span…
            </p>
          ) : (
            selectedSpan && (
              <div className="mt-6 min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Span
                    </Label>
                    <p className="text-xs font-mono break-all">
                      {selectedSpan.span_id}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Parent
                    </Label>
                    <p className="text-xs font-mono break-all">
                      {selectedSpan.parent_span_id ?? "—"}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Run
                    </Label>
                    <p className="text-xs font-mono break-all">
                      {selectedSpan.run_id}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Type
                    </Label>
                    <Badge
                      variant="outline"
                      className={getTypeClassName(selectedSpan.span_type)}
                    >
                      {selectedSpan.span_type}
                    </Badge>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Status
                    </Label>
                    <Badge
                      variant="outline"
                      className={getStatusClassName(selectedSpan.status)}
                    >
                      {selectedSpan.status}
                    </Badge>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Iteration
                    </Label>
                    <p className="text-sm">{selectedSpan.iteration ?? "—"}</p>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Started
                    </Label>
                    <p className="text-sm">
                      {format(new Date(selectedSpan.started_at), "PPpp")}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Ended
                    </Label>
                    <p className="text-sm">
                      {selectedSpan.ended_at
                        ? format(new Date(selectedSpan.ended_at), "PPpp")
                        : "—"}
                    </p>
                  </div>
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      Duration
                    </Label>
                    <p className="text-sm">
                      {formatDuration(selectedSpan.duration_ms)}
                    </p>
                  </div>
                </div>

                {selectedSpan.span_type === "agent" && (
                  <div className="space-y-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="w-full justify-start gap-1.5"
                      onClick={() =>
                        router.push(
                          `/dashboard/eval?run_id=${encodeURIComponent(selectedSpan.run_id)}`,
                        )
                      }
                    >
                      <FlaskConical className="size-4" />
                      Evaluate this run
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="w-full justify-start gap-1.5"
                      onClick={() => void toggleChildSpans()}
                      disabled={childrenLoading}
                    >
                      {childrenLoading ? (
                        "Loading child spans…"
                      ) : childSpans !== null ? (
                        <>
                          <ChevronDown className="size-4" />
                          Hide child spans
                        </>
                      ) : (
                        <>
                          <ChevronRight className="size-4" />
                          Show child spans
                        </>
                      )}
                    </Button>

                    {childrenError && (
                      <p className="text-xs text-onSurface-danger-primary">
                        {childrenError}
                      </p>
                    )}

                    {childSpans !== null && (
                      <div className="space-y-1">
                        <Label className="text-xs text-onSurface-default-tertiary">
                          Child spans ({childSpans.length})
                        </Label>
                        {childSpans.length === 0 ? (
                          <p className="text-xs text-onSurface-default-tertiary">
                            No child spans were captured for this run.
                          </p>
                        ) : (
                          <div className="divide-y divide-memBorderPrimary overflow-hidden rounded-md border border-memBorder-primary">
                            {childSpans.map((child) => (
                              <button
                                key={child.id}
                                type="button"
                                onClick={() => void openChildSpan(child)}
                                className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left transition-colors hover:bg-surface-default-secondary"
                              >
                                <div className="flex min-w-0 items-center gap-2">
                                  <Badge
                                    variant="outline"
                                    className={getTypeClassName(
                                      child.span_type,
                                    )}
                                  >
                                    {child.span_type}
                                  </Badge>
                                  <span
                                    className="truncate font-mono text-xs"
                                    title={child.name}
                                  >
                                    {child.name}
                                  </span>
                                </div>
                                <div className="flex shrink-0 items-center gap-2">
                                  <Badge
                                    variant="outline"
                                    className={getStatusClassName(child.status)}
                                  >
                                    {child.status}
                                  </Badge>
                                  <span className="w-16 text-right text-xs text-onSurface-default-tertiary">
                                    {formatDuration(child.duration_ms)}
                                  </span>
                                </div>
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {selectedSpan.error && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-danger-primary">
                      Error
                    </Label>
                    <pre className="max-h-40 overflow-auto rounded-md bg-surface-default-secondary p-3 text-xs font-mono whitespace-pre-wrap break-all text-onSurface-danger-primary">
                      {selectedSpan.error}
                    </pre>
                  </div>
                )}

                <JsonBlock label="Input" value={selectedSpan.input} />
                <JsonBlock label="Output" value={selectedSpan.output} />
                <JsonBlock label="Attributes" value={selectedSpan.attributes} />
              </div>
            )
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}

export default function TracePage() {
  return (
    <Suspense fallback={null}>
      <TracePageInner />
    </Suspense>
  );
}
