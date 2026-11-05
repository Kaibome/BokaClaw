"""Read-only access to agent execution spans for the dashboard.

Spans are written by the background runner (`app.agent.spans`); nothing here
mutates them. The list endpoint returns summary columns only: `input`,
`output` and `attributes` can each be up to `AGENT_SPAN_MAX_BYTES`, so they
are served by the detail endpoint instead.
"""

import uuid
from datetime import datetime

from app.core.auth import require_admin
from app.core.db import get_db
from app.models import AgentRunSpan
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

router = APIRouter(prefix="/traces", tags=["traces"])

SPAN_TYPE_PATTERN = "^(agent|llm|tool)$"
SPAN_STATUS_PATTERN = "^(running|completed|failed|aborted|interrupted)$"


class TraceSpanItem(BaseModel):
    id: int
    run_id: uuid.UUID
    span_id: str
    parent_span_id: str | None
    span_type: str
    name: str
    status: str
    iteration: int | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    error: str | None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cache_hit_rate: float | None = None

    model_config = {"from_attributes": True}


def _span_usage(attributes: object) -> tuple[int | None, int | None, int | None, float | None]:
    """Read (total, cached, missed, hit_rate) from a root span's usage.

    The runner stores a usage snapshot (``coverage``/``total_tokens``/sources)
    on the root span; older spans may carry the raw provider usage instead.
    Cache counters are optional: providers that support prompt caching report
    them, everything else leaves them out.
    """

    if not isinstance(attributes, dict):
        return None, None, None, None
    usage = attributes.get("usage")
    if not isinstance(usage, dict):
        return None, None, None, None

    total = usage.get("total_tokens")
    if isinstance(total, int):
        total_tokens: int | None = total
    else:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total_tokens = None
        if isinstance(prompt, int) or isinstance(completion, int):
            total_tokens = (prompt if isinstance(prompt, int) else 0) + (
                completion if isinstance(completion, int) else 0
            )

    cached = usage.get("cached_tokens")
    missed = usage.get("cache_miss_tokens")
    if not isinstance(cached, int) or not isinstance(missed, int):
        return total_tokens, None, None, None

    hit_rate = round(cached / (cached + missed), 4) if cached + missed > 0 else None
    return total_tokens, cached, missed, hit_rate


class TraceSpanDetail(TraceSpanItem):
    input: dict
    output: dict
    attributes: dict


@router.get("", response_model=list[TraceSpanItem])
def list_traces(
    _auth=Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=500),
    run_id: uuid.UUID | None = Query(default=None),
    span_type: str | None = Query(default=None, pattern=SPAN_TYPE_PATTERN),
    status: str | None = Query(default=None, pattern=SPAN_STATUS_PATTERN),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
):
    stmt = select(AgentRunSpan)
    if run_id is not None:
        stmt = stmt.where(AgentRunSpan.run_id == run_id)
    if span_type is not None:
        stmt = stmt.where(AgentRunSpan.span_type == span_type)
    if status is not None:
        stmt = stmt.where(AgentRunSpan.status == status)

    if order == "asc":
        stmt = stmt.order_by(AgentRunSpan.started_at.asc(), AgentRunSpan.id.asc())
    else:
        stmt = stmt.order_by(AgentRunSpan.started_at.desc(), AgentRunSpan.id.desc())

    spans = list(db.execute(stmt.limit(limit)).scalars().all())
    items: list[TraceSpanItem] = []
    for span in spans:
        item = TraceSpanItem.model_validate(span)
        if span.span_type == "agent":
            (
                item.total_tokens,
                item.cached_tokens,
                item.cache_miss_tokens,
                item.cache_hit_rate,
            ) = _span_usage(span.attributes)
        items.append(item)
    return items


@router.get("/{span_pk}", response_model=TraceSpanDetail)
def get_trace(
    span_pk: int,
    _auth=Depends(require_admin),
    db: Session = Depends(get_db),
):
    span = db.get(AgentRunSpan, span_pk)
    if span is None:
        raise HTTPException(status_code=404, detail="Span not found")
    return span
