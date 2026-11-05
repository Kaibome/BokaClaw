"""Online evaluation API.

The server only validates, enqueues and reads results; deepeval runs in the
isolated ``bokaclaw-eval`` worker because its pydantic requirement conflicts
with the server's pin. See ``docs/eval/eval-console-design.md``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.db import get_db
from app.eval.config import load_eval_config
from app.eval.goldens import (
    GoldenPayload,
    ToolCallPayload,
    build_snapshot,
    delete_golden,
    get_golden,
    list_goldens,
    snapshot_hash,
    upsert_golden,
)
from app.eval.repo import EvalRepository
from app.models import EVAL_STATUSES, TERMINAL_RUN_STATUSES, AgentRun, AgentRunSpan, EvalGolden, EvalRun, User

router = APIRouter(prefix="/evals", tags=["evals"])

INPUT_PREVIEW_CHARS = 200


class EvalCreateRequest(BaseModel):
    run_id: uuid.UUID
    eval_case_id: str = Field(min_length=1, max_length=255)
    use_llm_judge: bool = False


class MetricResult(BaseModel):
    name: str
    score: float | None = None
    threshold: float | None = None
    success: bool | None = None
    reason: str | None = None
    evaluation_model: str | None = None
    evaluation_cost: float | None = None
    skipped: bool = False
    error: str | None = None


class EvalRunItem(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    run_status: str
    eval_case_id: str
    status: str
    use_llm_judge: bool
    overall_success: bool | None = None
    judge_model: str | None = None
    token_cost: float | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None

    model_config = {"from_attributes": True}


class EvalRunDetail(EvalRunItem):
    golden_hash: str
    attempt: int
    warnings: list[str] = Field(default_factory=list)
    metrics: list[MetricResult] = Field(default_factory=list)


class EvalConfigResponse(BaseModel):
    enabled: bool
    judge_enabled: bool
    judge_provider: str
    judge_model: str
    golden_count: int
    max_attempts: int
    lease_seconds: int


class EvalGoldenItem(BaseModel):
    eval_case_id: str
    input_preview: str
    expected_tools: list[str]
    updated_at: datetime


class EvalGoldenUpsert(BaseModel):
    input: str = Field(min_length=1)
    expected_output: str | None = None
    expected_tools: list[ToolCallPayload] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    notes: str | None = None


class EvalGoldenCreate(EvalGoldenUpsert):
    eval_case_id: str = Field(min_length=1, max_length=255)


class EvalGoldenDetail(EvalGoldenUpsert):
    eval_case_id: str
    created_at: datetime
    updated_at: datetime


def _error(status_code: int, code: str, message: str, **extra) -> None:
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message, **extra})


def _created_by(user: User) -> uuid.UUID | None:
    # The ADMIN_API_KEY bootstrap admin is not a users row; the FK would reject it.
    if user.id == uuid.UUID(int=0):
        return None
    return user.id


def _item(row: EvalRun, run_status: str) -> EvalRunItem:
    return EvalRunItem(
        id=row.id,
        run_id=row.run_id,
        run_status=run_status,
        eval_case_id=row.eval_case_id,
        status=row.status,
        use_llm_judge=row.use_llm_judge,
        overall_success=row.overall_success,
        judge_model=row.judge_model,
        token_cost=row.token_cost,
        duration_ms=row.duration_ms,
        error_code=row.error_code,
        error_detail=row.error_detail,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        cancel_requested_at=row.cancel_requested_at,
    )


def _golden_detail(row: EvalGolden) -> EvalGoldenDetail:
    return EvalGoldenDetail(
        eval_case_id=row.eval_case_id,
        input=row.input,
        expected_output=row.expected_output,
        expected_tools=[ToolCallPayload.model_validate(tool) for tool in row.expected_tools or []],
        context=list(row.context or []),
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _golden_payload(eval_case_id: str, body: EvalGoldenUpsert) -> GoldenPayload:
    return GoldenPayload(
        eval_case_id=eval_case_id,
        input=body.input,
        expected_output=body.expected_output,
        expected_tools=body.expected_tools,
        context=body.context,
        notes=body.notes,
    )


def _detail(row: EvalRun, run_status: str) -> EvalRunDetail:
    return EvalRunDetail(
        **_item(row, run_status).model_dump(),
        golden_hash=row.golden_hash,
        attempt=row.attempt,
        warnings=list(row.warnings or []),
        metrics=[MetricResult.model_validate(metric) for metric in row.metrics or []],
    )


@router.get("/config", response_model=EvalConfigResponse)
def get_eval_config(
    _auth: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    config = load_eval_config()
    golden_count = db.scalar(select(func.count(EvalGolden.eval_case_id))) or 0
    return EvalConfigResponse(
        enabled=config.enabled,
        judge_enabled=config.judge_enabled,
        judge_provider=config.judge_provider,
        judge_model=config.judge_model,
        golden_count=golden_count,
        max_attempts=config.max_attempts,
        lease_seconds=config.lease_seconds,
    )


@router.get("/goldens", response_model=list[EvalGoldenItem])
def get_eval_goldens(
    _auth: User = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(default=500, ge=1, le=500),
):
    return [
        EvalGoldenItem(
            eval_case_id=row.eval_case_id,
            input_preview=(
                row.input
                if len(row.input) <= INPUT_PREVIEW_CHARS
                else row.input[:INPUT_PREVIEW_CHARS] + "…"
            ),
            expected_tools=[
                str(tool.get("name"))
                for tool in row.expected_tools or []
                if isinstance(tool, dict) and tool.get("name")
            ],
            updated_at=row.updated_at,
        )
        for row in list_goldens(db, limit=limit)
    ]


@router.get("/goldens/{eval_case_id}", response_model=EvalGoldenDetail, response_model_exclude_none=True)
def get_eval_golden(
    eval_case_id: str,
    _auth: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = get_golden(db, eval_case_id)
    if row is None:
        _error(404, "eval_golden_not_found", f"Golden {eval_case_id!r} does not exist.", eval_case_id=eval_case_id)
    return _golden_detail(row)


@router.post("/goldens", response_model=EvalGoldenDetail, status_code=201, response_model_exclude_none=True)
def create_eval_golden(
    body: EvalGoldenCreate,
    _auth: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if get_golden(db, body.eval_case_id) is not None:
        _error(
            409,
            "eval_golden_exists",
            f"Golden {body.eval_case_id!r} already exists.",
            eval_case_id=body.eval_case_id,
        )
    row = upsert_golden(db, _golden_payload(body.eval_case_id, body))
    return _golden_detail(row)


@router.put("/goldens/{eval_case_id}", response_model=EvalGoldenDetail, response_model_exclude_none=True)
def update_eval_golden(
    eval_case_id: str,
    body: EvalGoldenUpsert,
    _auth: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if get_golden(db, eval_case_id) is None:
        _error(404, "eval_golden_not_found", f"Golden {eval_case_id!r} does not exist.", eval_case_id=eval_case_id)
    row = upsert_golden(db, _golden_payload(eval_case_id, body))
    return _golden_detail(row)


@router.delete("/goldens/{eval_case_id}", status_code=204, response_class=Response)
def delete_eval_golden(
    eval_case_id: str,
    _auth: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    if not delete_golden(db, eval_case_id):
        _error(404, "eval_golden_not_found", f"Golden {eval_case_id!r} does not exist.", eval_case_id=eval_case_id)
    return Response(status_code=204)


@router.post("", response_model=EvalRunItem, status_code=202)
def create_eval(
    body: EvalCreateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    config = load_eval_config()
    if not config.enabled:
        _error(503, "eval_disabled", "Evaluation is disabled (EVAL_ENABLED=false).")

    run = db.get(AgentRun, body.run_id)
    if run is None:
        _error(404, "agent_run_not_found", f"Run {body.run_id} does not exist.", run_id=str(body.run_id))
    if run.status not in TERMINAL_RUN_STATUSES:
        _error(
            409,
            "agent_run_not_terminal",
            f"Run {body.run_id} is {run.status}; only terminal runs can be evaluated.",
            run_id=str(body.run_id),
            status=run.status,
        )

    span_count = db.scalar(
        select(func.count(AgentRunSpan.id)).where(AgentRunSpan.run_id == body.run_id)
    ) or 0
    if span_count == 0:
        _error(
            422,
            "eval_no_spans",
            "The run has no persisted spans (capture disabled or unsampled).",
            run_id=str(body.run_id),
        )

    golden = get_golden(db, body.eval_case_id)
    if golden is None:
        _error(
            404,
            "eval_golden_not_found",
            f"Golden {body.eval_case_id!r} does not exist.",
            eval_case_id=body.eval_case_id,
        )

    payload = run.request_payload or {}
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    run_case_id = metadata.get("eval_case_id") if isinstance(metadata, dict) else None
    if run_case_id and str(run_case_id) != body.eval_case_id:
        _error(
            409,
            "eval_case_mismatch",
            f"Run {body.run_id} is tagged {run_case_id!r}, not {body.eval_case_id!r}.",
            run_id=str(body.run_id),
            eval_case_id=body.eval_case_id,
            run_eval_case_id=str(run_case_id),
        )

    if body.use_llm_judge:
        if not config.judge_enabled:
            _error(422, "eval_judge_disabled", "The LLM judge is disabled (EVAL_JUDGE_ENABLED=false).")
        if not config.judge_available:
            _error(
                503,
                "eval_judge_unavailable",
                "The LLM judge is enabled but no judge key is configured.",
            )

    snapshot = build_snapshot(
        GoldenPayload.model_validate(
            {
                "eval_case_id": golden.eval_case_id,
                "input": golden.input,
                "expected_output": golden.expected_output,
                "expected_tools": golden.expected_tools,
                "context": golden.context,
            }
        )
    )

    row, duplicate = EvalRepository().create(
        run_id=body.run_id,
        created_by=_created_by(user),
        eval_case_id=body.eval_case_id,
        golden_snapshot=snapshot,
        golden_hash=snapshot_hash(snapshot),
        use_llm_judge=body.use_llm_judge,
    )
    if duplicate:
        _error(
            409,
            "eval_already_running",
            "An eval for this run, case and judge mode is already queued or running.",
            eval_id=str(row.id) if row is not None else None,
        )
    return _item(row, run.status)


@router.get("", response_model=list[EvalRunItem])
def list_evals(
    _auth: User = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=200),
    run_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None, pattern=f"^({'|'.join(EVAL_STATUSES)})$"),
    eval_case_id: str | None = Query(default=None, max_length=255),
):
    rows = EvalRepository().list(
        limit=limit,
        run_id=run_id,
        status=status,
        eval_case_id=eval_case_id,
    )
    return [_item(row, run_status) for row, run_status in rows]


@router.get("/{eval_id}", response_model=EvalRunDetail)
def get_eval(
    eval_id: uuid.UUID,
    _auth: User = Depends(require_admin),
):
    found = EvalRepository().get_with_run_status(eval_id)
    if found is None:
        _error(404, "eval_not_found", f"Eval {eval_id} does not exist.", eval_id=str(eval_id))
    row, run_status = found
    return _detail(row, run_status)


@router.post("/{eval_id}/cancel", response_model=EvalRunItem, status_code=202)
def cancel_eval(
    eval_id: uuid.UUID,
    _auth: User = Depends(require_admin),
):
    row, outcome = EvalRepository().cancel(eval_id)
    if outcome == "not_found" or row is None:
        _error(404, "eval_not_found", f"Eval {eval_id} does not exist.", eval_id=str(eval_id))
    if outcome == "terminal":
        _error(
            409,
            "eval_not_cancellable",
            f"Eval {eval_id} is already {row.status}.",
            eval_id=str(eval_id),
            status=row.status,
        )
    run = EvalRepository().get_run(row.run_id)
    return _item(row, run.status if run is not None else "unknown")
