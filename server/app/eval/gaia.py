"""GAIA benchmark helpers: record loading, answer extraction and scoring.

This module is intentionally deepeval-free so the server, import scripts and
unit tests can use it. The eval worker appends the resulting metric row to the
normal deepeval rows.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.eval.goldens import GoldenPayload

GAIA_BENCHMARK = "gaia"
GAIA_SCORER = "gaia_exact_match_v1"
GAIA_METRIC_NAME = "GAIA Exact Match"

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_SPREADSHEET_SUFFIXES = {".xlsx", ".xls", ".ods"}
_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".py", ".html"}
_ANSWER_MARKER = re.compile(
    r"(?i)^\s*[*_`#>:\-\s]*(?:final\s*answer|answer|答案)\s*[:：]\s*(.*)$"
)
_CODE_FENCE = re.compile(r"^\s*```")
_TRAILING_PUNCT = " \t\r\n.,;:!?。，；：！？、"


@dataclass(frozen=True)
class GaiaAttachment:
    name: str
    source_path: str
    workspace_path: str
    kind: str


@dataclass(frozen=True)
class GaiaRecord:
    task_id: str
    question: str
    answer: str
    level: int
    attachments: tuple[GaiaAttachment, ...] = ()


@dataclass(frozen=True)
class GaiaScore:
    success: bool
    extracted_answer: str | None
    normalized_actual: str
    normalized_expected: str
    reason: str


@dataclass
class GaiaSelection:
    records: list[GaiaRecord] = field(default_factory=list)
    skipped_unsupported: int = 0


def is_gaia_metadata(metadata: Any) -> bool:
    return isinstance(metadata, dict) and metadata.get("benchmark") == GAIA_BENCHMARK


def classify_attachment(name: str | None) -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _SPREADSHEET_SUFFIXES:
        return "spreadsheet"
    if suffix == ".pdf":
        return "pdf"
    if suffix in _TEXT_SUFFIXES or not suffix:
        return "text"
    return "other"


def _clean_answer_text(value: str) -> str:
    text = value.strip()
    text = text.strip("*_`#> \t")
    text = text.strip("\"'“”‘’")
    return text.strip(_TRAILING_PUNCT)


def extract_final_answer(text: str | None) -> str | None:
    if not text:
        return None
    lines = [line for line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        match = _ANSWER_MARKER.match(line)
        if not match:
            continue
        answer = _clean_answer_text(match.group(1))
        if answer:
            return answer
        for following in lines[index + 1 :]:
            candidate = _clean_answer_text(following)
            if candidate and not _CODE_FENCE.match(following):
                return candidate
    for line in reversed(lines):
        if not line.strip() or _CODE_FENCE.match(line):
            continue
        candidate = _clean_answer_text(line)
        if candidate:
            return candidate
    return None


def _strip_articles(text: str) -> str:
    tokens = text.split()
    if len(tokens) > 1 and tokens[0] in {"a", "an", "the"}:
        return " ".join(tokens[1:])
    return text


def normalize_answer(text: str | None) -> str:
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).casefold()
    value = re.sub(r"(?<=\d),(?=\d)", "", value)
    value = value.replace("$", " ").replace("€", " ").replace("£", " ").replace("¥", " ")
    value = value.replace("%", " ")
    value = re.sub(r"(?<!\d)\.(?!\d)", " ", value)
    value = re.sub(r"[^\w\s.]", " ", value, flags=re.UNICODE)
    value = " ".join(value.split())
    return _strip_articles(value).strip()


def _as_number(text: str) -> float | None:
    value = normalize_answer(text).replace(",", "").strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


def answers_match(actual: str | None, expected: str | None) -> bool:
    actual_normalized = normalize_answer(actual)
    expected_normalized = normalize_answer(expected)
    if not actual_normalized or not expected_normalized:
        return False
    if actual_normalized == expected_normalized:
        return True
    actual_number = _as_number(actual_normalized)
    expected_number = _as_number(expected_normalized)
    if actual_number is not None and expected_number is not None:
        return math.isclose(actual_number, expected_number, rel_tol=1e-6, abs_tol=1e-3)
    return False


def score_answer(actual: str | None, expected: str | None) -> GaiaScore:
    extracted = extract_final_answer(actual)
    normalized_expected = normalize_answer(expected)
    normalized_actual = normalize_answer(extracted)
    if not expected or not str(expected).strip():
        return GaiaScore(
            success=False,
            extracted_answer=extracted,
            normalized_actual=normalized_actual,
            normalized_expected=normalized_expected,
            reason="GAIA expected answer is missing.",
        )
    if extracted is None:
        return GaiaScore(
            success=False,
            extracted_answer=None,
            normalized_actual="",
            normalized_expected=normalized_expected,
            reason="未能从实际输出中提取到最终答案。",
        )
    matched = answers_match(extracted, expected)
    if matched:
        reason = f"答案匹配：{extracted!r} == {expected!r}"
    else:
        reason = f"答案不匹配：期望 {expected!r}，实际 {extracted!r}"
    return GaiaScore(
        success=matched,
        extracted_answer=extracted,
        normalized_actual=normalized_actual,
        normalized_expected=normalized_expected,
        reason=reason,
    )


def gaia_metric_row(actual: str | None, expected: str | None) -> dict[str, Any]:
    if not expected or not str(expected).strip():
        return {
            "name": GAIA_METRIC_NAME,
            "score": None,
            "threshold": 1.0,
            "success": None,
            "reason": "GAIA expected answer is missing.",
            "details": None,
            "skipped": True,
            "error": None,
        }
    score = score_answer(actual, expected)
    return {
        "name": GAIA_METRIC_NAME,
        "score": 1.0 if score.success else 0.0,
        "threshold": 1.0,
        "success": score.success,
        "reason": score.reason,
        "details": {
            "scorer": GAIA_SCORER,
            "extracted_answer": score.extracted_answer,
            "normalized_actual": score.normalized_actual,
            "normalized_expected": score.normalized_expected,
        },
        "skipped": False,
        "error": None,
    }


def build_question_input(record: GaiaRecord) -> str:
    parts = [record.question.strip()]
    if record.attachments:
        lines = ["Attached file(s), relative to the workspace:"]
        lines.extend(f"- `{attachment.workspace_path}`" for attachment in record.attachments)
        parts.append("\n".join(lines))
    parts.append("When you have the answer, end your reply with a line exactly like:\nFinal answer: <your answer>")
    return "\n\n".join(parts)


def record_to_golden(record: GaiaRecord, *, split: str = "validation") -> GoldenPayload:
    metadata = {
        "benchmark": GAIA_BENCHMARK,
        "scorer": GAIA_SCORER,
        "split": split,
        "task_id": record.task_id,
        "level": record.level,
        "attachments": [
            {
                "name": attachment.name,
                "source_path": attachment.source_path,
                "workspace_path": attachment.workspace_path,
                "kind": attachment.kind,
            }
            for attachment in record.attachments
        ],
    }
    return GoldenPayload(
        eval_case_id=f"gaia-{split}-{record.task_id}",
        input=build_question_input(record),
        expected_output=record.answer,
        expected_tools=[],
        context=[],
        metadata=metadata,
        notes=f"GAIA {split}, level {record.level}, task {record.task_id}.",
    )


def _row_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _parse_level(value: Any) -> int | None:
    try:
        level = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return level if level in {1, 2, 3} else None


def _record_from_row(row: dict[str, Any], *, split: str) -> GaiaRecord | None:
    task_id = str(_row_value(row, "task_id", "taskId", "id") or "").strip()
    question = str(_row_value(row, "Question", "question") or "").strip()
    answer = str(_row_value(row, "Final answer", "final_answer", "answer") or "").strip()
    level = _parse_level(_row_value(row, "Level", "level"))
    if not task_id or not question or not answer or level is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", task_id):
        return None

    file_name = str(_row_value(row, "file_name", "fileName") or "").strip()
    file_path = str(_row_value(row, "file_path", "filePath") or "").strip()
    attachments: list[GaiaAttachment] = []
    if file_name or file_path:
        name = Path(file_name or file_path).name
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            return None
        attachments.append(
            GaiaAttachment(
                name=name,
                source_path=file_path or name,
                workspace_path=f"gaia/{task_id}/{name}",
                kind=classify_attachment(name),
            )
        )
    return GaiaRecord(
        task_id=task_id,
        question=question,
        answer=answer,
        level=level,
        attachments=tuple(attachments),
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def load_gaia_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return _read_jsonl(path)
    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value = value.get("data") or value.get("rows") or [value]
        if not isinstance(value, list):
            raise ValueError("GAIA JSON must be an array of objects.")
        return [row for row in value if isinstance(row, dict)]
    if suffix in {".parquet", ".pq"}:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ValueError(
                "Reading GAIA Parquet requires pyarrow; export JSONL on the host instead."
            ) from exc
        return [dict(row) for row in pq.read_table(path).to_pylist()]
    raise ValueError(f"Unsupported GAIA file type: {path.suffix}")


def select_gaia_records(
    rows: Iterable[dict[str, Any]],
    *,
    split: str = "validation",
    levels: Iterable[int] | None = None,
    task_ids: Iterable[str] | None = None,
    limit: int | None = None,
    sample: str = "stratified",
    include_unsupported: bool = False,
) -> GaiaSelection:
    level_filter = {int(level) for level in levels} if levels is not None else None
    task_filter = {str(task_id) for task_id in task_ids} if task_ids is not None else None
    selection = GaiaSelection()
    seen: set[str] = set()
    records: list[GaiaRecord] = []
    for row in rows:
        record = _record_from_row(row, split=split)
        if record is None or record.task_id in seen:
            continue
        seen.add(record.task_id)
        if level_filter is not None and record.level not in level_filter:
            continue
        if task_filter is not None and record.task_id not in task_filter:
            continue
        if not include_unsupported and any(
            attachment.kind in {"image", "spreadsheet", "other"} for attachment in record.attachments
        ):
            selection.skipped_unsupported += 1
            continue
        records.append(record)

    records.sort(key=lambda record: (record.level, record.task_id))
    if limit is not None and limit >= 0 and len(records) > limit:
        if sample == "first":
            records = records[:limit]
        else:
            by_level: dict[int, list[GaiaRecord]] = {}
            for record in records:
                by_level.setdefault(record.level, []).append(record)
            picked: list[GaiaRecord] = []
            while len(picked) < limit:
                advanced = False
                for level in sorted(by_level):
                    if len(picked) >= limit:
                        break
                    if by_level[level]:
                        picked.append(by_level[level].pop(0))
                        advanced = True
                if not advanced:
                    break
            records = picked
    selection.records = records
    return selection
