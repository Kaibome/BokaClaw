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
import type { MessageKey } from "@/i18n/messages/en";
import { useI18n } from "@/i18n/provider";
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

const EVAL_STATUS_KEYS: Record<EvalStatus, MessageKey> = {
  queued: "enums.evalStatus.queued",
  running: "enums.evalStatus.running",
  completed: "enums.evalStatus.completed",
  failed: "enums.evalStatus.failed",
  cancelled: "enums.evalStatus.cancelled",
};

const RUN_STATUS_KEYS: Record<RunStatus, MessageKey> = {
  queued: "enums.runStatus.queued",
  running: "enums.runStatus.running",
  completed: "enums.runStatus.completed",
  failed: "enums.runStatus.failed",
  cancelled: "enums.runStatus.cancelled",
  interrupted: "enums.runStatus.interrupted",
};

const METRIC_STATE_KEYS: Record<string, MessageKey> = {
  skipped: "eval.metric.state.skipped",
  error: "eval.metric.state.error",
  pass: "eval.metric.state.pass",
  fail: "eval.metric.state.fail",
};

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
  const { t } = useI18n();
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
            {t(METRIC_STATE_KEYS[state])}
          </Badge>
        </div>
        <span className="text-xs text-onSurface-default-secondary">
          {t("eval.metric.score", {
            score: formatScore(metric.score),
            threshold: formatScore(metric.threshold),
          })}
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
          <span>
            {t("eval.metric.judge", { model: metric.evaluation_model })}
          </span>
        )}
        {metric.evaluation_cost !== null && (
          <span>
            {t("eval.metric.cost", {
              cost: formatCost(metric.evaluation_cost),
            })}
          </span>
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
  const { t, formatRelativeTime } = useI18n();
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
            {t("eval.detail.evalLabel", { id: detail.id })}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge
            variant="outline"
            className={getStatusClassName(detail.status)}
          >
            {cancelPending
              ? t("eval.cancellingState")
              : t(EVAL_STATUS_KEYS[detail.status])}
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
              {cancelPending ? t("eval.cancelling") : t("eval.cancel")}
            </Button>
          )}
        </div>
      </div>
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              {t("eval.detail.overall")}
            </p>
            <p className="font-medium">
              {detail.overall_success === null
                ? "—"
                : detail.overall_success
                  ? t("eval.pass")
                  : t("eval.fail")}
            </p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              {t("eval.detail.runStatus")}
            </p>
            <p className="font-medium">
              {RUN_STATUS_KEYS[detail.run_status as RunStatus]
                ? t(RUN_STATUS_KEYS[detail.run_status as RunStatus])
                : detail.run_status}
            </p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              {t("eval.detail.duration")}
            </p>
            <p className="font-medium">
              {detail.duration_ms === null ? "—" : `${detail.duration_ms} ms`}
            </p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              {t("eval.detail.judgeModel")}
            </p>
            <p className="font-medium">{detail.judge_model ?? "—"}</p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              {t("eval.detail.judgeCost")}
            </p>
            <p className="font-medium">{formatCost(detail.token_cost)}</p>
          </div>
          <div>
            <p className="text-xs text-onSurface-default-tertiary">
              {t("eval.detail.created")}
            </p>
            <p className="font-medium">
              {formatRelativeTime(detail.created_at)}
            </p>
          </div>
        </div>

        <div className="space-y-1">
          <Label className="text-xs text-onSurface-default-tertiary">
            {t("eval.detail.runCase")}
          </Label>
          <p className="break-all font-mono text-xs">
            {detail.run_id} / {detail.eval_case_id}
          </p>
          <Link
            href={`/dashboard/trace?run_id=${detail.run_id}`}
            className="inline-block text-xs text-primary underline-offset-4 hover:underline"
          >
            {t("eval.detail.openInTrace")}
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
              {t("eval.detail.metrics")}
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
              {t("eval.detail.warnings")}
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
  const { t, formatDateTime, formatRelativeTime } = useI18n();
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
    { errorToast: t("eval.errors.loadConfig") },
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
    { errorToast: t("eval.errors.loadHistory"), initialData: [] },
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
          title: t("eval.errors.loadEval"),
          description: getErrorMessage(err, t("common.error.generic")),
          variant: "destructive",
        });
      }
    },
    [loadDetail, t],
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
          setPollError(t("eval.errors.pollTimeout"));
          setActiveEvalId(null);
          return;
        }
        timer = setTimeout(() => void poll(), POLL_MS);
      } catch (err) {
        if (cancelled) return;
        setPollError(getErrorMessage(err, t("eval.errors.loadEval")));
        timer = setTimeout(() => void poll(), POLL_MS * 2);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeEvalId, refetchHistory, t]);

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
        throw new Error(t("eval.errors.agentTimeout"));
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
    throw new Error(t("eval.errors.closed"));
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedCase = caseId.trim();
    if (!trimmedCase) {
      toast({ title: t("eval.errors.caseRequired"), variant: "destructive" });
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
        fallbacks: {
          emptyResponse: t("eval.errors.emptyResponse"),
          startFailed: t("eval.errors.startFailed"),
          noRunId: t("eval.errors.noRunId"),
        },
      });
      if (!mountedRef.current) return;
      setAgentRunId(newRunId);

      // 3. Wait for the run to reach a terminal status.
      const run = await waitForRunTerminal(newRunId);
      if (!mountedRef.current) return;
      if (run.status !== "completed") {
        const suffix = run.errorCode ? ` (${run.errorCode})` : "";
        setAgentRunError(
          t("eval.errors.runStatus", { status: run.status, suffix }),
        );
        toast({
          title: t("eval.errors.agentNotComplete"),
          description: t("eval.errors.notQueued"),
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
        title: t("eval.errors.runFailed"),
        description: getErrorMessage(err, t("common.error.generic")),
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
        title: t("eval.errors.cancelFailed"),
        description: getErrorMessage(err, t("common.error.generic")),
        variant: "destructive",
      });
    } finally {
      setCancelling(false);
    }
  };

  const historyColumns = [
    {
      key: "created_at" as keyof EvalRunItem,
      label: t("eval.table.time"),
      width: 120,
      render: (value: string) => (
        <span title={formatDateTime(value)}>{formatRelativeTime(value)}</span>
      ),
    },
    {
      key: "eval_case_id" as keyof EvalRunItem,
      label: t("eval.table.case"),
      width: 200,
      render: (value: string) => (
        <span className="font-mono text-xs break-all">{value}</span>
      ),
    },
    {
      key: "status" as keyof EvalRunItem,
      label: t("eval.table.status"),
      width: 100,
      render: (value: EvalStatus) => (
        <Badge variant="outline" className={getStatusClassName(value)}>
          {t(EVAL_STATUS_KEYS[value])}
        </Badge>
      ),
    },
    {
      key: "overall_success" as keyof EvalRunItem,
      label: t("eval.table.overall"),
      width: 80,
      render: (value: boolean | null) =>
        value === null ? "—" : value ? t("eval.pass") : t("eval.fail"),
    },
    {
      key: "use_llm_judge" as keyof EvalRunItem,
      label: t("eval.table.judge"),
      width: 70,
      render: (value: boolean) => (value ? t("eval.on") : t("eval.off")),
    },
    {
      key: "duration_ms" as keyof EvalRunItem,
      label: t("eval.table.duration"),
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
            {t("eval.title")}
          </h1>
          <p className="text-sm text-onSurface-default-secondary">
            {t("eval.description")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="outline" asChild>
            <Link href="/dashboard/eval/cases">
              <FolderCog className="mr-2 size-4" />
              {t("eval.caseManagement")}
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
            {t("eval.refresh")}
          </Button>
        </div>
      </div>

      {config && !config.enabled && (
        <Card className="border-memBorder-primary">
          <CardContent className="p-4 text-sm text-onSurface-danger-primary">
            {t("eval.disabled")}
          </CardContent>
        </Card>
      )}

      <Card className="border-memBorder-primary">
        <CardContent className="p-4">
          <form className="space-y-4" onSubmit={submit}>
            <div className="space-y-1">
              <Label htmlFor="eval-case">{t("eval.caseLabel")}</Label>
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
                {t("eval.caseHint")}
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
                  {t("eval.useJudge")}{" "}
                  {judgeDisabled && (
                    <span className="text-onSurface-default-tertiary">
                      {t("eval.unavailable")}
                    </span>
                  )}
                </Label>
                <p className="text-xs text-onSurface-default-tertiary">
                  {t("eval.judgeHintPrefix")}
                  {config
                    ? `${config.judge_provider} / ${config.judge_model}`
                    : t("eval.judgeHintFallback")}
                  {t("eval.judgeHintSuffix")}
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
                    {phase === "running"
                      ? t("eval.phase.runningAgent")
                      : t("eval.phase.evaluating")}
                  </Badge>
                )}
                {agentRunId && (
                  <Link
                    href={`/dashboard/trace?run_id=${agentRunId}`}
                    className="text-xs text-onSurface-default-secondary underline underline-offset-2 hover:text-onSurface-default-primary"
                  >
                    {t("eval.viewTrace")}
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
                  ? t("eval.run.running")
                  : phase === "evaluating"
                    ? t("eval.run.queuing")
                    : t("eval.run.run")}
              </Button>
            </div>

            {goldens.length === 0 && (
              <p className="text-xs text-onSurface-default-tertiary">
                {t("eval.noGoldensPrefix")}
                <code className="font-mono">scripts/seed_eval_goldens.py</code>
                {t("eval.noGoldensSuffix")}
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
            <DialogTitle>{t("eval.result.title")}</DialogTitle>
            <DialogDescription>
              {detail
                ? t("eval.result.description", {
                    caseId: detail.eval_case_id,
                    status: t(EVAL_STATUS_KEYS[detail.status]),
                  })
                : activeEvalId
                  ? t("eval.result.queued", { id: activeEvalId })
                  : t("eval.result.loading")}
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
                ? t("eval.result.queued", { id: activeEvalId })
                : t("eval.result.loading")}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold font-fustat">
            {t("eval.recent")}
          </h2>
          <span className="text-xs text-onSurface-default-tertiary">
            {t("eval.recent.last", { count: HISTORY_LIMIT })}
          </span>
        </div>
        <Card className="overflow-hidden border-memBorderPrimary">
          {history.length === 0 ? (
            <EmptyState
              title={t("eval.empty.title")}
              description={t("eval.empty.description")}
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
