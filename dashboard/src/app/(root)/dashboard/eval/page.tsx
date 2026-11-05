"use client";

import {
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { format, formatDistanceToNow } from "date-fns";
import {
  FlaskConical,
  FolderCog,
  Loader2,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/use-toast";
import { DataTable } from "@/components/shared/data-table";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { getErrorMessage } from "@/lib/error-message";
import { useApiQuery } from "@/hooks/use-api-query";
import { api } from "@/utils/api";
import { startAgentRun } from "@/utils/agent-run";
import { AGENT_ENDPOINTS, EVAL_ENDPOINTS } from "@/utils/api-endpoints";
import {
  AgentRun,
  EvalConfig,
  EvalGolden,
  EvalGoldenDetail,
  EvalMetricResult,
  EvalRunDetail,
  EvalRunItem,
  EvalStatus,
  RunStatus,
} from "@/types/api";

const POLL_MS = 1500;
const POLL_TIMEOUT_MS = 15 * 60 * 1000;
const HISTORY_LIMIT = 50;

const isTerminal = (status: EvalStatus) =>
  status === "completed" || status === "failed" || status === "cancelled";

const isRunTerminal = (status: RunStatus) =>
  status === "completed" ||
  status === "failed" ||
  status === "cancelled" ||
  status === "interrupted";

const getRunStatusClassName = (status: RunStatus) => {
  switch (status) {
    case "completed":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "failed":
      return "border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-900/40 dark:bg-rose-950/40 dark:text-rose-300";
    case "cancelled":
    case "interrupted":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900/40 dark:bg-amber-950/40 dark:text-amber-300";
    case "running":
      return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-900/40 dark:bg-sky-950/40 dark:text-sky-300";
    case "queued":
      return "border-neutral-200 bg-neutral-50 text-neutral-700 dark:border-neutral-800 dark:bg-neutral-900/40 dark:text-neutral-300";
  }
};

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
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h3 className="flex items-center gap-2 text-base font-semibold">
            <FlaskConical className="size-4" />
            {detail.eval_case_id}
          </h3>
          <p className="text-xs break-all text-onSurface-default-tertiary">
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
      </div>
      <div className="space-y-4">
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
      </div>
    </div>
  );
}

function EvalPageInner() {
  const searchParams = useSearchParams();
  const [caseId, setCaseId] = useState(
    () => searchParams.get("eval_case_id")?.trim() ?? "",
  );
  const [useJudge, setUseJudge] = useState(false);
  const [activeEvalId, setActiveEvalId] = useState<string | null>(null);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [phase, setPhase] = useState<"idle" | "running" | "evaluating">("idle");
  const [cancelling, setCancelling] = useState(false);
  const [pollError, setPollError] = useState("");
  const [agentRunId, setAgentRunId] = useState<string | null>(null);
  const [agentRunError, setAgentRunError] = useState("");
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

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

  const openDetail = useCallback(
    async (id: string) => {
      setDetailOpen(true);
      setDetail(null);
      try {
        await loadDetail(id);
      } catch (err) {
        setDetailOpen(false);
        toast({
          title: "Failed to load eval",
          description: getErrorMessage(err),
          variant: "destructive",
        });
      }
    },
    [loadDetail],
  );

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

  // The eval API only accepts terminal runs, so the run started for this eval
  // must finish before it can be scored.
  const waitForRunTerminal = async (id: string) => {
    const startedAt = Date.now();
    while (mountedRef.current) {
      const res = await api.get<AgentRun>(AGENT_ENDPOINTS.RUN(id));
      if (isRunTerminal(res.data.status)) {
        return { status: res.data.status, errorCode: res.data.error_code };
      }
      if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
        throw new Error(
          "Timed out waiting for the agent run to finish. Open Trace to check it.",
        );
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
    throw new Error("The eval page was closed before the run finished.");
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedCase = caseId.trim();
    if (!trimmedCase) {
      toast({ title: "Eval case is required", variant: "destructive" });
      return;
    }
    if (phase !== "idle") return;

    setPhase("running");
    setPollError("");
    setAgentRunId(null);
    setAgentRunError("");
    try {
      // 1. Load the full golden input (the list API only returns a preview).
      const goldenRes = await api.get<EvalGoldenDetail>(
        EVAL_ENDPOINTS.GOLDEN_BY_ID(trimmedCase),
      );
      const golden = goldenRes.data;

      // 2. Run the case in a new conversation, tagged with this case.
      const newRunId = await startAgentRun({
        message: golden.input,
        metadata: { eval_case_id: golden.eval_case_id },
      });
      if (!mountedRef.current) return;
      setAgentRunId(newRunId);

      // 3. Wait for the run to reach a terminal status.
      const run = await waitForRunTerminal(newRunId);
      if (!mountedRef.current) return;
      if (run.status !== "completed") {
        const suffix = run.errorCode ? ` (${run.errorCode})` : "";
        setAgentRunError(`Agent run ${run.status}${suffix}.`);
        toast({
          title: "Agent run did not complete",
          description: "No eval was queued. Open Trace for details.",
          variant: "destructive",
        });
        return;
      }

      // 4. Score the completed run against the same golden.
      setPhase("evaluating");
      const res = await api.post<EvalRunItem>(EVAL_ENDPOINTS.BASE, {
        run_id: newRunId,
        eval_case_id: golden.eval_case_id,
        use_llm_judge: useJudge,
      });
      if (!mountedRef.current) return;
      setDetail(null);
      setDetailOpen(true);
      setActiveEvalId(res.data.id);
      void refetchHistory();
    } catch (err) {
      if (!mountedRef.current) return;
      toast({
        title: "Failed to run eval",
        description: getErrorMessage(err),
        variant: "destructive",
      });
    } finally {
      if (mountedRef.current) setPhase("idle");
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
            Run a golden case through the agent, then score the result using its
            captured spans.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" asChild>
            <Link href="/dashboard/eval/cases">
              <FolderCog className="mr-2 size-4" />
              Case management
            </Link>
          </Button>
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
                  <option key={golden.eval_case_id} value={golden.eval_case_id}>
                    {golden.input_preview}
                  </option>
                ))}
              </datalist>
              <p className="text-xs text-onSurface-default-tertiary">
                Runs this case&apos;s golden input in a new conversation, waits
                for the run to finish, then evaluates that run.
              </p>
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

            {agentRunError && (
              <p className="text-xs text-onSurface-danger-primary">
                {agentRunError}
              </p>
            )}

            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                {phase !== "idle" && (
                  <Badge
                    variant="outline"
                    className={getRunStatusClassName("running")}
                  >
                    {phase === "running" ? "running agent" : "evaluating"}
                  </Badge>
                )}
                {agentRunId && (
                  <Link
                    href={`/dashboard/trace?run_id=${agentRunId}`}
                    className="text-xs text-onSurface-default-secondary underline underline-offset-2 hover:text-onSurface-default-primary"
                  >
                    View trace
                  </Link>
                )}
              </div>
              <Button
                type="submit"
                disabled={phase !== "idle" || config?.enabled === false}
              >
                {phase !== "idle" && (
                  <Loader2 className="mr-1.5 size-4 animate-spin" />
                )}
                {phase === "running"
                  ? "Running agent…"
                  : phase === "evaluating"
                    ? "Queuing eval…"
                    : "Run eval"}
              </Button>
            </div>

            {goldens.length === 0 && (
              <p className="text-xs text-onSurface-default-tertiary">
                No goldens in the database yet; seed them with{" "}
                <code className="font-mono">scripts/seed_eval_goldens.py</code>.
              </p>
            )}
          </form>
        </CardContent>
      </Card>

      <Dialog
        open={detailOpen}
        onOpenChange={(open) => {
          setDetailOpen(open);
          if (!open) {
            setActiveEvalId(null);
            void refetchHistory();
          }
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Eval result</DialogTitle>
            <DialogDescription>
              {detail
                ? `${detail.eval_case_id} · ${detail.status}`
                : activeEvalId
                  ? `Eval ${activeEvalId} is queued. Waiting for the worker…`
                  : "Loading eval…"}
            </DialogDescription>
          </DialogHeader>
          {detail ? (
            <EvalDetailCard
              detail={detail}
              cancelling={cancelling}
              onCancel={() => void cancel()}
            />
          ) : (
            <div className="flex items-center gap-2 py-6 text-sm text-onSurface-default-secondary">
              <RefreshCw className="size-4 animate-spin" />
              {activeEvalId
                ? `Eval ${activeEvalId} is queued. Waiting for the worker…`
                : "Loading eval…"}
            </div>
          )}
        </DialogContent>
      </Dialog>

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
              onRowClick={(row) => void openDetail(row.id)}
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
