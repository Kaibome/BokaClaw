"use client";

import { Suspense, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FlaskConical,
  RefreshCw,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import type { MessageKey } from "@/i18n/messages/en";
import { useI18n } from "@/i18n/provider";
import {
  AgentRunSpanDetail,
  AgentRunSpanSummary,
  SpanStatus,
  SpanType,
} from "@/types/api";

const TRACE_LIMIT = 500;
const PAGE_SIZE = 20;

type StatusFilter = SpanStatus | "all";

const SPAN_TYPE_KEYS: Record<SpanType, MessageKey> = {
  agent: "enums.spanType.agent",
  llm: "enums.spanType.llm",
  tool: "enums.spanType.tool",
  memory: "enums.spanType.memory",
  compression: "enums.spanType.compression",
};

const SPAN_STATUS_KEYS: Record<SpanStatus, MessageKey> = {
  running: "enums.spanStatus.running",
  completed: "enums.spanStatus.completed",
  failed: "enums.spanStatus.failed",
  aborted: "enums.spanStatus.aborted",
  interrupted: "enums.spanStatus.interrupted",
};

const getTypeClassName = (spanType: SpanType) => {
  switch (spanType) {
    case "agent":
      return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900/40 dark:bg-sky-950/40 dark:text-sky-300";
    case "llm":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "tool":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/40 dark:bg-amber-950/40 dark:text-amber-300";
    case "memory":
      return "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-900/40 dark:bg-violet-950/40 dark:text-violet-300";
    case "compression":
      return "border-fuchsia-200 bg-fuchsia-50 text-fuchsia-700 dark:border-fuchsia-900/40 dark:bg-fuchsia-950/40 dark:text-fuchsia-300";
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

const SpanDetailFields = ({
  span,
  children,
}: {
  span: AgentRunSpanDetail;
  children?: ReactNode;
}) => {
  const { t, formatDateTime } = useI18n();

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.span")}
          </Label>
          <p className="text-xs font-mono break-all">{span.span_id}</p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.parent")}
          </Label>
          <p className="text-xs font-mono break-all">
            {span.parent_span_id ?? "—"}
          </p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.run")}
          </Label>
          <p className="text-xs font-mono break-all">{span.run_id}</p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.type")}
          </Label>
          <Badge variant="outline" className={getTypeClassName(span.span_type)}>
            {t(SPAN_TYPE_KEYS[span.span_type])}
          </Badge>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.status")}
          </Label>
          <Badge variant="outline" className={getStatusClassName(span.status)}>
            {t(SPAN_STATUS_KEYS[span.status])}
          </Badge>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.iteration")}
          </Label>
          <p className="text-sm">{span.iteration ?? "—"}</p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.started")}
          </Label>
          <p className="text-sm">{formatDateTime(span.started_at)}</p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.ended")}
          </Label>
          <p className="text-sm">
            {span.ended_at ? formatDateTime(span.ended_at) : "—"}
          </p>
        </div>
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("trace.fields.duration")}
          </Label>
          <p className="text-sm">{formatDuration(span.duration_ms)}</p>
        </div>
      </div>

      {children}

      {span.error && (
        <div className="space-y-1">
          <Label className="text-xs text-onSurface-danger-primary">
            {t("trace.fields.error")}
          </Label>
          <pre className="max-h-40 overflow-auto rounded-md bg-surface-default-secondary p-3 text-xs font-mono whitespace-pre-wrap break-all text-onSurface-danger-primary">
            {span.error}
          </pre>
        </div>
      )}

      <JsonBlock label={t("trace.fields.input")} value={span.input} />
      <JsonBlock label={t("trace.fields.output")} value={span.output} />
      <JsonBlock label={t("trace.fields.attributes")} value={span.attributes} />
    </div>
  );
};

const SpanList = ({
  spans,
  onSelect,
}: {
  spans: AgentRunSpanSummary[];
  onSelect: (span: AgentRunSpanSummary) => void;
}) => {
  const { t } = useI18n();

  return (
    <div className="divide-y divide-memBorderPrimary overflow-hidden rounded-md border border-memBorder-primary">
      {spans.map((span) => (
        <button
          key={span.id}
          type="button"
          onClick={() => onSelect(span)}
          className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left transition-colors hover:bg-surface-default-secondary"
        >
          <div className="flex min-w-0 items-center gap-2">
            <Badge
              variant="outline"
              className={`w-12 justify-center ${getTypeClassName(span.span_type)}`}
            >
              {t(SPAN_TYPE_KEYS[span.span_type])}
            </Badge>
            <span className="truncate font-mono text-xs" title={span.name}>
              {span.name}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Badge
              variant="outline"
              className={getStatusClassName(span.status)}
            >
              {t(SPAN_STATUS_KEYS[span.status])}
            </Badge>
            <span className="w-16 text-right text-xs text-onSurface-default-tertiary">
              {formatDuration(span.duration_ms)}
            </span>
          </div>
        </button>
      ))}
    </div>
  );
};

function TracePageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { t, formatNumber, formatDateTime, formatRelativeTime } = useI18n();
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
  // Every span captured for the selected run. Loaded once when the child list
  // is opened and reused for depth-first navigation, so drilling into nested
  // subagent spans does not issue another request.
  const [runSpans, setRunSpans] = useState<AgentRunSpanSummary[] | null>(null);
  const [childrenLoading, setChildrenLoading] = useState(false);
  const [childrenError, setChildrenError] = useState<string | null>(null);
  // Drill-down path inside the child dialog; the last entry is the open span.
  const [childStack, setChildStack] = useState<AgentRunSpanSummary[]>([]);
  const [childDetail, setChildDetail] = useState<AgentRunSpanDetail | null>(
    null,
  );
  const [childDetailLoading, setChildDetailLoading] = useState(false);
  const detailRequestRef = useRef(0);
  const childDetailRequestRef = useRef(0);
  const childDetailCacheRef = useRef(new Map<number, AgentRunSpanDetail>());

  const currentChild = childStack[childStack.length - 1] ?? null;

  const childSpans = useMemo(() => {
    if (!runSpans || !selectedSpan) return null;
    return runSpans
      .filter((span) => span.parent_span_id === selectedSpan.span_id)
      .sort((a, b) => a.started_at.localeCompare(b.started_at));
  }, [runSpans, selectedSpan]);

  const currentChildSpans = useMemo(() => {
    if (!runSpans || !currentChild) return [];
    return runSpans
      .filter((span) => span.parent_span_id === currentChild.span_id)
      .sort((a, b) => a.started_at.localeCompare(b.started_at));
  }, [runSpans, currentChild]);

  const childPath = useMemo(() => {
    if (!selectedSpan) return "";
    return [selectedSpan.name, ...childStack.map((span) => span.name)].join(
      " / ",
    );
  }, [selectedSpan, childStack]);

  const {
    data: spans = [],
    isLoading,
    error,
    refetch,
  } = useApiQuery<AgentRunSpanSummary[]>(
    async () => {
      const res = await api.get<AgentRunSpanSummary[]>(TRACE_ENDPOINTS.BASE, {
        // Nested agent spans (subagents) hang off a parent tool call; only root
        // spans represent a run and should consume the list limit.
        params: { limit: TRACE_LIMIT, span_type: "agent", roots_only: true },
      });
      setLastUpdated(new Date().toISOString());
      return res.data ?? [];
    },
    { errorToast: t("trace.failedToLoad"), initialData: [] },
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
  const spansWithTokens = filteredSpans.filter(
    (span) => typeof span.total_tokens === "number",
  );
  const totalTokens = spansWithTokens.reduce(
    (sum, span) => sum + (span.total_tokens ?? 0),
    0,
  );
  const cachedTokens = filteredSpans.reduce(
    (sum, span) => sum + (span.cached_tokens ?? 0),
    0,
  );
  const uncachedTokens = filteredSpans.reduce(
    (sum, span) => sum + (span.cache_miss_tokens ?? 0),
    0,
  );
  const cacheHitRate =
    cachedTokens + uncachedTokens > 0
      ? Math.round((cachedTokens / (cachedTokens + uncachedTokens)) * 100)
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
          title: t("trace.errors.loadSpan"),
          description: getErrorMessage(err, t("common.error.generic")),
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
    setRunSpans(null);
    setChildrenError(null);
    setChildStack([]);
    setChildDetail(null);
    childDetailCacheRef.current.clear();
    await loadSpanDetail(row);
  };

  const loadChildDetail = async (child: AgentRunSpanSummary) => {
    const cached = childDetailCacheRef.current.get(child.id);
    if (cached) {
      childDetailRequestRef.current += 1;
      setChildDetail(cached);
      setChildDetailLoading(false);
      return;
    }

    const requestId = ++childDetailRequestRef.current;
    setChildDetail(null);
    setChildDetailLoading(true);
    try {
      const res = await api.get<AgentRunSpanDetail>(
        TRACE_ENDPOINTS.BY_ID(child.id),
      );
      if (requestId === childDetailRequestRef.current) {
        childDetailCacheRef.current.set(child.id, res.data);
        setChildDetail(res.data);
      }
    } catch (err) {
      if (requestId === childDetailRequestRef.current) {
        toast({
          title: t("trace.errors.loadChildSpan"),
          description: getErrorMessage(err, t("common.error.generic")),
          variant: "destructive",
        });
        setChildStack([]);
        setChildDetail(null);
      }
    } finally {
      if (requestId === childDetailRequestRef.current) {
        setChildDetailLoading(false);
      }
    }
  };

  const openChildSpan = async (child: AgentRunSpanSummary) => {
    setChildStack((stack) => [...stack, child]);
    await loadChildDetail(child);
  };

  const goBackChildSpan = () => {
    const next = childStack.slice(0, -1);
    setChildStack(next);
    const parent = next[next.length - 1];
    if (parent) {
      void loadChildDetail(parent);
    } else {
      childDetailRequestRef.current += 1;
      setChildDetail(null);
      setChildDetailLoading(false);
    }
  };

  const toggleChildSpans = async () => {
    if (!selectedSpan) return;
    if (runSpans !== null) {
      setRunSpans(null);
      return;
    }
    setChildrenLoading(true);
    setChildrenError(null);
    try {
      // Load every span in the run once. The root's direct children render
      // immediately and every opened span resolves its own children from this
      // list, so drilling into subagent spans costs no extra requests.
      const res = await api.get<AgentRunSpanSummary[]>(TRACE_ENDPOINTS.BASE, {
        params: { run_id: selectedSpan.run_id, limit: TRACE_LIMIT },
      });
      setRunSpans(res.data ?? []);
    } catch (err) {
      setChildrenError(getErrorMessage(err, t("common.error.generic")));
    } finally {
      setChildrenLoading(false);
    }
  };

  const columns = [
    {
      key: "started_at" as keyof AgentRunSpanSummary,
      label: t("trace.table.time"),
      width: 140,
      render: (value: string) => (
        <span title={formatDateTime(value)}>{formatRelativeTime(value)}</span>
      ),
    },
    {
      key: "run_id" as keyof AgentRunSpanSummary,
      label: t("trace.table.run"),
      width: 110,
      render: (value: string) => (
        <span className="font-mono text-xs" title={value}>
          {value.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "name" as keyof AgentRunSpanSummary,
      label: t("trace.table.name"),
      width: 240,
      render: (value: string) => (
        <span className="font-mono text-xs break-all">{value}</span>
      ),
    },
    {
      key: "status" as keyof AgentRunSpanSummary,
      label: t("trace.table.status"),
      width: 110,
      render: (value: SpanStatus) => (
        <Badge variant="outline" className={getStatusClassName(value)}>
          {t(SPAN_STATUS_KEYS[value])}
        </Badge>
      ),
    },
    {
      key: "iteration" as keyof AgentRunSpanSummary,
      label: t("trace.table.iteration"),
      width: 80,
      align: "right" as const,
      render: (value: number | null) => (value === null ? "—" : value),
    },
    {
      key: "duration_ms" as keyof AgentRunSpanSummary,
      label: t("trace.table.duration"),
      width: 100,
      align: "right" as const,
      render: (value: number | null) => formatDuration(value),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold font-fustat">
            {t("trace.title")}
          </h1>
          <p className="text-sm text-onSurface-default-secondary">
            {t("trace.description")}
          </p>
          {lastUpdated && (
            <p className="text-xs text-onSurface-default-tertiary">
              {t("trace.lastUpdated", {
                time: formatRelativeTime(lastUpdated),
              })}
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
          {t("trace.refresh")}
        </Button>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        {[
          { label: t("trace.stats.runs"), value: totalRuns, hint: undefined },
          {
            label: t("trace.stats.avgDuration"),
            value: averageDuration === null ? "--" : `${averageDuration} ms`,
            hint: undefined,
          },
          {
            label: t("trace.stats.tokens"),
            value:
              spansWithTokens.length > 0 ? formatNumber(totalTokens) : "--",
            hint:
              cacheHitRate === null
                ? undefined
                : t("trace.stats.cacheHit", { percent: cacheHitRate }),
          },
          {
            label: t("trace.stats.errors"),
            value:
              totalSpans > 0
                ? `${failedSpans} (${Math.round((failedSpans / totalSpans) * 100)}%)`
                : "--",
            hint: undefined,
          },
        ].map((card) => (
          <Card key={card.label} className="border-memBorder-primary">
            <CardContent className="p-5">
              <p className="text-xs text-onSurface-default-tertiary">
                {card.label}
              </p>
              <p className="mt-1 text-2xl font-semibold">{card.value}</p>
              {card.hint ? (
                <p className="text-xs text-onSurface-default-tertiary">
                  {card.hint}
                </p>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="border-memBorder-primary">
        <CardContent className="flex flex-col gap-3 p-4 md:flex-row md:items-end">
          <div className="w-full space-y-1 md:max-w-xs">
            <Label htmlFor="trace-run">{t("trace.filters.runId")}</Label>
            <Input
              id="trace-run"
              placeholder={t("trace.filters.runIdPlaceholder")}
              value={runId}
              onChange={(event) => {
                setRunId(event.target.value);
                setPage(0);
              }}
            />
          </div>
          <div className="w-full space-y-1 md:w-44">
            <Label>{t("trace.filters.status")}</Label>
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
                <SelectItem value="all">
                  {t("trace.filters.allStatuses")}
                </SelectItem>
                <SelectItem value="running">
                  {t("enums.spanStatus.running")}
                </SelectItem>
                <SelectItem value="completed">
                  {t("enums.spanStatus.completed")}
                </SelectItem>
                <SelectItem value="failed">
                  {t("enums.spanStatus.failed")}
                </SelectItem>
                <SelectItem value="aborted">
                  {t("enums.spanStatus.aborted")}
                </SelectItem>
                <SelectItem value="interrupted">
                  {t("enums.spanStatus.interrupted")}
                </SelectItem>
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
          title={t("trace.empty.title")}
          description={t("trace.empty.description")}
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
                {t("trace.range", {
                  start: page * PAGE_SIZE + 1,
                  end: Math.min((page + 1) * PAGE_SIZE, filteredSpans.length),
                  total: filteredSpans.length,
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
                  disabled={(page + 1) * PAGE_SIZE >= filteredSpans.length}
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
        open={!!selectedSpan}
        onOpenChange={(open) => {
          if (!open) {
            detailRequestRef.current += 1;
            childDetailRequestRef.current += 1;
            setSelectedSpan(null);
            setRunSpans(null);
            setChildrenError(null);
            setChildStack([]);
            setChildDetail(null);
            childDetailCacheRef.current.clear();
          }
        }}
      >
        <SheetContent className="flex flex-col sm:max-w-lg">
          <SheetHeader className="shrink-0">
            <SheetTitle>{t("trace.spanDetail.title")}</SheetTitle>
            <SheetDescription className="sr-only">
              {t("trace.spanDetail.description")}
            </SheetDescription>
          </SheetHeader>
          {detailLoading ? (
            <p className="mt-6 text-sm text-onSurface-default-secondary">
              {t("trace.loadingSpan")}
            </p>
          ) : (
            selectedSpan && (
              <div className="mt-6 min-h-0 flex-1 overflow-y-auto pr-1">
                <SpanDetailFields span={selectedSpan}>
                  {selectedSpan.span_type === "agent" && (
                    <div className="space-y-2">
                      {typeof selectedSpan.input.eval_case_id === "string" &&
                        selectedSpan.input.eval_case_id.length > 0 && (
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            className="w-full justify-start gap-1.5"
                            onClick={() =>
                              router.push(
                                `/dashboard/eval?eval_case_id=${encodeURIComponent(String(selectedSpan.input.eval_case_id))}`,
                              )
                            }
                          >
                            <FlaskConical className="size-4" />
                            {t("trace.evaluateCase")}
                          </Button>
                        )}
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="w-full justify-start gap-1.5"
                        onClick={() => void toggleChildSpans()}
                        disabled={childrenLoading}
                      >
                        {childrenLoading ? (
                          t("trace.childSpans.loading")
                        ) : runSpans !== null ? (
                          <>
                            <ChevronDown className="size-4" />
                            {t("trace.childSpans.hide")}
                          </>
                        ) : (
                          <>
                            <ChevronRight className="size-4" />
                            {t("trace.childSpans.show")}
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
                            {t("trace.childSpans.label", {
                              count: childSpans.length,
                            })}
                          </Label>
                          {childSpans.length === 0 ? (
                            <p className="text-xs text-onSurface-default-tertiary">
                              {t("trace.childSpans.empty")}
                            </p>
                          ) : (
                            <SpanList
                              spans={childSpans}
                              onSelect={(child) => void openChildSpan(child)}
                            />
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </SpanDetailFields>
              </div>
            )
          )}
        </SheetContent>
      </Sheet>

      <Dialog
        open={childStack.length > 0}
        onOpenChange={(open) => {
          if (!open) {
            childDetailRequestRef.current += 1;
            setChildStack([]);
            setChildDetail(null);
            setChildDetailLoading(false);
          }
        }}
      >
        <DialogContent className="grid max-h-[85vh] max-w-2xl grid-rows-[auto_minmax(0,1fr)] overflow-hidden">
          <DialogHeader>
            <DialogTitle>
              {currentChild?.name ?? t("trace.childDetail.title")}
            </DialogTitle>
            <DialogDescription className="font-mono text-xs break-all">
              {childPath}
            </DialogDescription>
          </DialogHeader>
          <div className="min-h-0 overflow-y-auto pr-1">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="-ml-2 mb-2 gap-1"
              onClick={goBackChildSpan}
            >
              <ChevronLeft className="size-4" />
              {t("trace.childDetail.back")}
            </Button>
            {childDetailLoading ? (
              <p className="text-sm text-onSurface-default-secondary">
                {t("trace.loadingSpan")}
              </p>
            ) : childDetail ? (
              <SpanDetailFields span={childDetail}>
                {runSpans !== null && (
                  <div className="space-y-1">
                    <Label className="text-xs text-onSurface-default-tertiary">
                      {t("trace.childSpans.label", {
                        count: currentChildSpans.length,
                      })}
                    </Label>
                    {currentChildSpans.length === 0 ? (
                      <p className="text-xs text-onSurface-default-tertiary">
                        {t("trace.childDetail.empty")}
                      </p>
                    ) : (
                      <SpanList
                        spans={currentChildSpans}
                        onSelect={(child) => void openChildSpan(child)}
                      />
                    )}
                  </div>
                )}
              </SpanDetailFields>
            ) : null}
          </div>
        </DialogContent>
      </Dialog>
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
