"""Golden validation, snapshotting and database access.

The database is the runtime source of truth. A golden is frozen into
``eval_runs.golden_snapshot`` at enqueue time so later edits cannot rewrite
history; the worker never re-reads ``eval_goldens``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import EvalGolden


class ToolCallPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    input_parameters: dict[str, Any] | None = None
    output: Any | None = None
    reasoning: str | None = None
    description: str | None = None
    type: str | None = None


class GoldenPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    eval_case_id: str = Field(min_length=1, max_length=255)
    input: str = Field(min_length=1)
    expected_output: str | None = None
    expected_tools: list[ToolCallPayload] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    notes: str | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_snapshot(payload: GoldenPayload) -> dict[str, Any]:
    """Reference fields only; maintenance notes are not part of the hash."""
    return {
        "eval_case_id": payload.eval_case_id,
        "input": payload.input,
        "expected_output": payload.expected_output,
        "expected_tools": [tool.model_dump(exclude_none=True) for tool in payload.expected_tools],
        "context": list(payload.context),
    }


def normalize_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(normalize_snapshot(snapshot).encode("utf-8")).hexdigest()


def golden_fields_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Fields accepted by ``deepeval.dataset.Golden`` (case id is ours)."""
    fields: dict[str, Any] = {"input": snapshot["input"]}
    if snapshot.get("expected_output") is not None:
        fields["expected_output"] = snapshot["expected_output"]
    if snapshot.get("expected_tools"):
        fields["expected_tools"] = snapshot["expected_tools"]
    if snapshot.get("context"):
        fields["context"] = snapshot["context"]
    return fields


def validate_snapshot(snapshot: dict[str, Any]) -> GoldenPayload:
    return GoldenPayload.model_validate(snapshot)


def validate_goldens_file(items: Any) -> list[GoldenPayload]:
    if not isinstance(items, list):
        raise ValueError("Goldens file must be a JSON array.")
    parsed: list[GoldenPayload] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"Golden #{index} must be an object.")
        try:
            parsed.append(GoldenPayload.model_validate(item))
        except Exception as exc:  # pydantic ValidationError, kept as-is for the CLI
            raise ValueError(f"Golden #{index} is invalid: {exc}") from exc
    return parsed


def get_golden(db: Session, eval_case_id: str) -> EvalGolden | None:
    return db.get(EvalGolden, eval_case_id)


def list_goldens(db: Session, limit: int = 500) -> list[EvalGolden]:
    return list(db.scalars(select(EvalGolden).order_by(EvalGolden.eval_case_id.asc()).limit(limit)).all())


def upsert_golden(db: Session, payload: GoldenPayload) -> EvalGolden:
    now = utcnow()
    row = db.get(EvalGolden, payload.eval_case_id)
    values = {
        "input": payload.input,
        "expected_output": payload.expected_output,
        "expected_tools": [tool.model_dump(exclude_none=True) for tool in payload.expected_tools],
        "context": list(payload.context),
        "notes": payload.notes,
    }
    if row is None:
        row = EvalGolden(eval_case_id=payload.eval_case_id, created_at=now, updated_at=now, **values)
        db.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = now
    db.commit()
    return row


def delete_golden(db: Session, eval_case_id: str) -> bool:
    row = db.get(EvalGolden, eval_case_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def delete_missing_goldens(db: Session, keep: set[str]) -> int:
    stmt = delete(EvalGolden)
    if keep:
        stmt = stmt.where(EvalGolden.eval_case_id.not_in(keep))
    result = db.execute(stmt)
    db.commit()
    return result.rowcount or 0
