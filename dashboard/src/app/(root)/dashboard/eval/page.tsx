"use client";

import {
  Suspense,
  useCallback,
  useEffect,
  useState,
  type FormEvent,
} from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { format, formatDistanceToNow } from "date-fns";
import { FlaskConical, RefreshCw, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/use-toast";
import { DataTable } from "@/components/shared/data-table";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { getErrorMessage } from "@/lib/error-message";
import { useApiQuery } from "@/hooks/use-api-query";
import { api } from "@/utils/api";
import { EVAL_ENDPOINTS } from "@/utils/api-endpoints";
import {
  EvalConfig,
  EvalGolden,
  EvalMetricResult,
  EvalRunDetail,
  EvalRunItem,
  EvalStatus,
} from "@/types/api";

const POLL_MS = 1500;
const POLL_TIMEOUT_MS = 15 * 60 * 1000;
const HISTORY_LIMIT = 50;

const isTerminal = (status: EvalStatus) =>
  status === "completed" || status === "failed" || status === "cancelled";

const getStatusClassName = (status: EvalStatus) => {
  switch (status) {
    case "completed":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "failed":
      return "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900/40 dark:bg-rose-950/40 dark:text-rose-300";
    case "cancelled":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/40 dark:bg-amber-950/40 dark:text-amber-300";
    case "running":
      return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900/40 dark:bg-sky-950/40 dark:text-sky-300";
    case "queued":
      return "border-neutral-200 bg-neutral-50 text-neutral-700 dark:border-neutral-800 dark:bg-neutral-900/40 dark:text-neutral-300";
  }
};

const formatCost = (value: number | null) =>
  value === null ? "—" : `$${value.toFixed(4)}`;

const formatScore = (value: number | null) =>
  value === null ? "—" : value.toFixed(2);

function MetricRow({ metric }: { metric: EvalMetricResult }) {
  const state = metric.skipped
    ? "skipped"
    : metric.error
      ? "error"
      : metric.success
        ? "pass"
        : "fail";
  const stateClass =
    state === "pass"
      ? "text-emerald-600 dark:text-emerald-400"
      : state === "fail"
        ? "text-rose-600 dark:text-rose-400"
        : state === "error"
          ? "text-rose-600 dark:text-rose-400"
          : "text-onSurface-default-tertiary";

  return (
    <div className="space-y-1 rounded-md border border-memBorder-primary p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs font-medium">{metric.name}</span>
          <Badge variant="outline" className={stateClass}>
            {state}
          </Badge>
        </div>
        <span className="text-xs text-onSurface-default-secondary">
          score {formatScore(metric.score)} / threshold{" "}
          {formatScore(metric.threshold)}
        </span>
      </div>
      {metric.reason && (
        <p className="text-xs text-onSurface-default-secondary">
          {metric.reason}
        </p>
      )}
      {metric.error && (
        <p className="text-xs text-onSurface-danger-primary">{metric.error}</p>
      )}
      <div className="flex flex-wrap gap-3 text-[11px] text-onSurface-default-tertiary">
        {metric.evaluation_model && (
          <span>judge: {metric.evaluation_model}</span>
        )}
        {metric.evaluation_cost !== null && (
          <span>cost: {formatCost(metric.evaluation_cost)}</span>
        )}
      </div>
    </div>
  );
}

function EvalDetailCard({
  detail,
  cancelling,
  onCancel,
}: {
  detail: EvalRunDetail;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const running = detail.status === "queued" || detail.status === "running";
  const cancelPending =
    detail.status === "running" && detail.cancel_requested_at !== null;

  return (
    <Card className="border-memBorder-primary">
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2 text-base">
            <FlaskConical className="size-4" />
            {detail.eval_case_id}
          </CardTitle>
          <p className="text-xs text-onSurface-default-tertiary">
            eval {detail.id}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge
            variant="outline"
            className={getStatusClassName(detail.status)}
          >
            {cancelPending ? "cancelling" : detail.status}
          </Badge>
          {running && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={onCancel}
              disabled={cancelling || cancelPending}
            >
              <XCircle className="mr-1.5 size-3.5" />
              {cancelPending ? "Cancelling…" : "Cancel"}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
          <div>
            <p className="text-xs text-onSurface-default-tertiary">Overall</p>
            <p className="font-medium">
              {detail.overall_success === null
                ? "—"
                : detail.overall_success
                  ? "PASS"
                  : "FAIL"}
            </p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              Run status
            </p>
            <p className="font-medium">{detail.run_status}</p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">Duration</p>
            <p className="font-medium">
              {detail.duration_ms === null ? "—" : `${detail.duration_ms} ms`}
            </p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              Judge model
            </p>
            <p className="font-medium">{detail.judge_model ?? "—"}</p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              Judge cost
            </p>
            <p className="font-medium">{formatCost(detail.token_cost)}</p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">Created</p>
            <p className="font-medium">
              {formatDistanceToNow(new Date(detail.created_at), {
                addSuffix: true,
              })}
            </p>
          </div>
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            Run / case
          </Label>
          <p className="break-all font-mono text-xs">
            {detail.run_id} / {detail.eval_case_id}
          </p>
          <Link
            href={`/dashboard/trace?run_id=${detail.run_id}`}
            className="inline-block text-xs text-primary underline-offset-4 hover:underline"
          >
            Open this run in Trace
          </Link>
        </div>

        {detail.error_code && (
          <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm dark:border-rose-900/40 dark:bg-rose-950/40">
            <p className="font-medium text-rose-700 dark:text-rose-300">
              {detail.error_code}
            </p>
            {detail.error_detail && (
              <p className="mt-1 text-xs text-rose-700/80 dark:text-rose-300/80">
                {detail.error_detail}
              </p>
            )}
          </div>
        )}

        {detail.metrics.length > 0 && (
          <div className="space-y-2">
            <Label className="text-xs text-onSurface-default-tertiary">
              Metrics
            </Label>
            <div className="space-y-2">
              {detail.metrics.map((metric) => (
                <MetricRow key={metric.name} metric={metric} />
              ))}
            </div>
          </div>
        )}

        {detail.warnings.length > 0 && (
          <div className="space-y-1">
            <Label className="text-xs text-onSurface-default-tertiary">
              Warnings
            </Label>
            <ul className="list-inside list-disc text-xs text-onSurface-default-secondary">
              {detail.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function EvalPageInner() {
  const searchParams = useSearchParams();
  const [runId, setRunId] = useState(
    () => searchParams.get("run_id")?.trim() ?? "",
  );
  const [caseId, setCaseId] = useState(
    () => searchParams.get("eval_case_id")?.trim() ?? "",
  );
  const [useJudge, setUseJudge] = useState(false);
  const [activeEvalId, setActiveEvalId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [pollError, setPollError] = useState("");

  const { data: config } = useApiQuery<EvalConfig>(
    async () => {
      const res = await api.get<EvalConfig>(EVAL_ENDPOINTS.CONFIG);
      return res.data;
    },
    { errorToast: "Failed to load eval configuration" },
  );

  const { data: goldens = [] } = useApiQuery<EvalGolden[]>(
    async () => {
      const res = await api.get<EvalGolden[]>(EVAL_ENDPOINTS.GOLDENS);
      return res.data ?? [];
    },
    { initialData: [] },
  );

  const {
    data: history = [],
    isLoading: historyLoading,
    refetch: refetchHistory,
  } = useApiQuery<EvalRunItem[]>(
    async () => {
      const res = await api.get<EvalRunItem[]>(EVAL_ENDPOINTS.BASE, {
        params: { limit: HISTORY_LIMIT },
      });
      return res.data ?? [];
    },
    { errorToast: "Failed to load eval history", initialData: [] },
  );

  const loadDetail = useCallback(async (id: string) => {
    const res = await api.get<EvalRunDetail>(EVAL_ENDPOINTS.BY_ID(id));
    setDetail(res.data);
    setActiveEvalId(isTerminal(res.data.status) ? null : id);
    return res.data;
  }, []);

  useEffect(() => {
    if (!activeEvalId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();

    const poll = async () => {
      try {
        const res = await api.get<EvalRunDetail>(
          EVAL_ENDPOINTS.BY_ID(activeEvalId),
        );
        if (cancelled) return;
        setDetail(res.data);
        setPollError("");
        if (isTerminal(res.data.status)) {
          setActiveEvalId(null);
          void refetchHistory();
          return;
        }
        if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
          setPollError(
            "Polling timed out. Refresh to check the eval again; it may still be running.",
          );
          setActiveEvalId(null);
          return;
        }
        timer = setTimeout(() => void poll(), POLL_MS);
      } catch (err) {
        if (cancelled) return;
        setPollError(getErrorMessage(err, "Failed to load eval"));
        timer = setTimeout(() => void poll(), POLL_MS * 2);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeEvalId, refetchHistory]);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedRun = runId.trim();
    const trimmedCase = caseId.trim();
    if (!trimmedRun || !trimmedCase) {
      toast({
        title: "Run ID and eval case are required",
        variant: "destructive",
      });
      return;
    }
    setSubmitting(true);
    setPollError("");
    try {
      const res = await api.post<EvalRunItem>(EVAL_ENDPOINTS.BASE, {
        run_id: trimmedRun,
        eval_case_id: trimmedCase,
        use_llm_judge: useJudge,
      });
      setDetail(null);
      setActiveEvalId(res.data.id);
      void refetchHistory();
    } catch (err) {
      toast({
        title: "Failed to start eval",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async () => {
    if (!detail) return;
    setCancelling(true);
    try {
      await api.post(EVAL_ENDPOINTS.CANCEL(detail.id));
      await loadDetail(detail.id);
      void refetchHistory();
    } catch (err) {
      toast({
        title: "Failed to cancel eval",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    } finally {
      setCancelling(false);
    }
  };

  const historyColumns = [
    {
      key: "created_at" as keyof EvalRunItem,
      label: "Time",
      width: 120,
      render: (value: string) => (
        <span title={format(new Date(value), "PPpp")}>
          {formatDistanceToNow(new Date(value), { addSuffix: true })}
        </span>
      ),
    },
    {
      key: "eval_case_id" as keyof EvalRunItem,
      label: "Case",
      width: 200,
      render: (value: string) => (
        <span className="font-mono text-xs break-all">{value}</span>
      ),
    },
    {
      key: "status" as keyof EvalRunItem,
      label: "Status",
      width: 100,
      render: (value: EvalStatus) => (
        <Badge variant="outline" className={getStatusClassName(value)}>
          {value}
        </Badge>
      ),
    },
    {
      key: "overall_success" as keyof EvalRunItem,
      label: "Overall",
      width: 80,
      render: (value: boolean | null) =>
        value === null ? "—" : value ? "PASS" : "FAIL",
    },
    {
      key: "use_llm_judge" as keyof EvalRunItem,
      label: "Judge",
      width: 70,
      render: (value: boolean) => (value ? "on" : "off"),
    },
    {
      key: "duration_ms" as keyof EvalRunItem,
      label: "Duration",
      width: 90,
      align: "right" as const,
      render: (value: number | null) => (value === null ? "—" : `${value} ms`),
    },
  ];

  const judgeDisabled = !config?.judge_enabled;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="flex items-center gap-2 text-xl font-semibold font-fustat">
            <FlaskConical className="size-5" />
            Eval
          </h1>
          <p className="text-sm text-onSurface-default-secondary">
            Score one persisted agent run against a golden case using its
            captured spans.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => {
            void refetchHistory();
            if (detail) void loadDetail(detail.id);
          }}
          disabled={historyLoading}
        >
          <RefreshCw className="mr-2 size-4" />
          Refresh
        </Button>
      </div>

      {config && !config.enabled && (
        <Card className="border-memBorder-primary">
          <CardContent className="p-4 text-sm text-onSurface-danger-primary">
            Evaluation is disabled on this deployment (EVAL_ENABLED=false).
          </CardContent>
        </Card>
      )}

      <Card className="border-memBorder-primary">
        <CardContent className="p-4">
          <form className="space-y-4" onSubmit={submit}>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="eval-run">Run ID</Label>
                <Input
                  id="eval-run"
                  placeholder="33ddd007-183a-4f6d-a1ad-698c6c5719e2"
                  value={runId}
                  onChange={(event) => setRunId(event.target.value)}
                  required
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="eval-case">Eval case</Label>
                <Input
                  id="eval-case"
                  list="eval-case-options"
                  placeholder="minimax-pricing-golden-01"
                  value={caseId}
                  onChange={(event) => setCaseId(event.target.value)}
                  required
                />
                <datalist id="eval-case-options">
                  {goldens.map((golden) => (
                    <option
                      key={golden.eval_case_id}
                      value={golden.eval_case_id}
                    >
                      {golden.input_preview}
                    </option>
                  ))}
                </datalist>
                {goldens.length === 0 && (
                  <p className="text-xs text-onSurface-default-tertiary">
                    No goldens in the database yet; seed them with{" "}
                    <code className="font-mono">
                      scripts/seed_eval_goldens.py
                    </code>
                    .
                  </p>
                )}
              </div>
            </div>

            <div className="flex items-start gap-2">
              <Checkbox
                id="eval-judge"
                checked={useJudge}
                disabled={judgeDisabled}
                onCheckedChange={(checked) => setUseJudge(checked === true)}
              />
              <div className="space-y-1">
                <Label htmlFor="eval-judge" className="text-sm">
                  Use the LLM judge{" "}
                  {judgeDisabled && (
                    <span className="text-onSurface-default-tertiary">
                      (unavailable)
                    </span>
                  )}
                </Label>
                <p className="text-xs text-onSurface-default-tertiary">
                  Off by default. When enabled, the truncated prompt, reply and
                  tool calls are sent to{" "}
                  {config
                    ? `${config.judge_provider} / ${config.judge_model}`
                    : "the configured judge model"}
                  , which costs tokens.
                </p>
              </div>
            </div>

            {pollError && (
              <p className="text-xs text-onSurface-danger-primary">
                {pollError}
              </p>
            )}

            <div className="flex justify-end">
              <Button
                type="submit"
                disabled={submitting || config?.enabled === false}
              >
                {submitting ? "Queuing…" : "Run eval"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {detail && (
        <EvalDetailCard
          detail={detail}
          cancelling={cancelling}
          onCancel={() => void cancel()}
        />
      )}
      {!detail && activeEvalId && (
        <Card className="border-memBorderPrimary">
          <CardContent className="flex items-center gap-2 p-4 text-sm text-onSurface-default-secondary">
            <RefreshCw className="size-4 animate-spin" />
            Eval {activeEvalId} is queued. Waiting for the worker…
          </CardContent>
        </Card>
      )}

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold font-fustat">Recent evals</h2>
          <span className="text-xs text-onSurface-default-tertiary">
            last {HISTORY_LIMIT}
          </span>
        </div>
        <Card className="overflow-hidden border-memBorderPrimary">
          {history.length === 0 ? (
            <EmptyState
              title="No evals yet"
              description="Run one above; results are kept in Postgres."
              image="requests"
            />
          ) : (
            <DataTable
              data={history}
              columns={historyColumns}
              getRowKey={(row) => row.id}
              onRowClick={(row) => {
                if (row.status === "queued" || row.status === "running") {
                  setDetail(null);
                  setActiveEvalId(row.id);
                  void loadDetail(row.id).catch((err: unknown) => {
                    toast({
                      title: "Failed to load eval",
                      description: getErrorMessage(err),
                      variant: "destructive",
                    });
                  });
                  return;
                }
                void loadDetail(row.id).catch((err: unknown) => {
                  toast({
                    title: "Failed to load eval",
                    description: getErrorMessage(err),
                    variant: "destructive",
                  });
                });
              }}
            />
          )}
        </Card>
      </div>
    </div>
  );
}

export default function EvalPage() {
  return (
    <Suspense fallback={null}>
      <EvalPageInner />
    </Suspense>
  );
}
