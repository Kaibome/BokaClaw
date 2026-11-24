"""Run DRBench tasks through BokaClaw's public HTTP API and capture reports.

Intended for the host (standard library plus the document readers used for
sidecar extraction):

    python server/scripts/run_drbench_benchmark.py \
      --base-url http://127.0.0.1:8000 \
      --data-root ~/BokaClaw/drbench_data \
      --workspace-host ~/BokaClaw/workspace \
      --limit 15 --out ~/BokaClaw/drbench_results/minival.json

Each task's env files are copied into ``workspace/drbench/<task_id>/``. DOCX,
XLSX, PPTX and JSONL files also get a readable ``.extracted.md`` sidecar; the
agent is told to cite the original destination path, not the sidecar.
Scoring is a separate step (``score_drbench_results.py``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
CASE_PREFIX = "drbench-"
SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_INSIGHTS_KEY = '"report_insights"'
_REPORT_TEXT_KEY = '"report_text"'


class ApiError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _request(url: str, *, method: str = "GET", body: dict[str, Any] | None = None, api_key: str | None, timeout: float = 60.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise ApiError(exc.code, raw.decode("utf-8", errors="replace")) from exc
    return json.loads(raw.decode("utf-8")) if raw else None


class Api:
    def __init__(self, base_url: str, api_key: str | None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def get(self, path: str, *, timeout: float = 60.0) -> Any:
        return _request(f"{self.base_url}{path}", api_key=self.api_key, timeout=timeout)

    def post(self, path: str, body: dict[str, Any], *, timeout: float = 60.0) -> Any:
        return _request(f"{self.base_url}{path}", method="POST", body=body, api_key=self.api_key, timeout=timeout)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _select_cases(cases: list[dict[str, Any]], *, case_ids: set[str] | None, limit: int) -> list[dict[str, Any]]:
    if case_ids:
        cases = [case for case in cases if case["eval_case_id"] in case_ids]
    return cases[:limit]


def _load_cases(api: Api, args: argparse.Namespace) -> list[dict[str, Any]]:
    items = api.get("/evals/goldens?limit=500") or []
    cases = [item for item in items if str(item.get("eval_case_id", "")).startswith(CASE_PREFIX)]
    case_ids = {part.strip() for part in args.case_ids.split(",") if part.strip()} if args.case_ids else None
    selected = _select_cases(cases, case_ids=case_ids, limit=args.limit)
    return [api.get(f"/evals/goldens/{_quote(case['eval_case_id'])}") for case in selected]


def _safe_destination(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe env destination: {value!r}")
    return path


def _docx_text(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    lines = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(lines)


def _xlsx_text(path: Path) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    lines: list[str] = []
    for sheet in workbook.worksheets:
        lines.append(f"## Sheet: {sheet.title}")
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= 200:
                lines.append("... [sheet truncated]")
                break
            values = ["" if value is None else str(value) for value in row]
            if any(values):
                lines.append(" | ".join(values))
    workbook.close()
    return "\n".join(lines)


def _pptx_text(path: Path) -> str:
    from pptx import Presentation

    presentation = Presentation(str(path))
    lines: list[str] = []
    for index, slide in enumerate(presentation.slides, 1):
        lines.append(f"## Slide {index}")
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text and text.strip():
                lines.append(text.strip())
    return "\n".join(lines)


def _jsonl_text(path: Path) -> str:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(raw)
            continue
        if isinstance(item, dict):
            lines.append("; ".join(f"{key}: {value}" for key, value in item.items()))
        else:
            lines.append(str(item))
    return "\n".join(lines)


_READERS = {".docx": _docx_text, ".xlsx": _xlsx_text, ".pptx": _pptx_text, ".jsonl": _jsonl_text}


def _materialize(case: dict[str, Any], *, data_root: Path, workspace_host: Path, subdir: str) -> Path:
    metadata = case.get("metadata") or {}
    task_id = str(metadata.get("task_id") or "")
    if not SAFE_TASK_ID.fullmatch(task_id):
        raise ValueError(f"unsafe DRBench task id: {task_id!r}")
    task_root = workspace_host / subdir / task_id
    if task_root.exists():
        shutil.rmtree(task_root)
    task_root.mkdir(parents=True)
    rows: list[str] = []
    for item in metadata.get("env_files") or []:
        source = str(item.get("source") or "")
        destination = _safe_destination(str(item.get("destination") or ""))
        origin = data_root / source
        if not origin.is_file():
            raise FileNotFoundError(f"env file not found: {source!r}")
        target = task_root / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
        sidecar = ""
        reader = _READERS.get(origin.suffix.lower())
        if reader is not None:
            try:
                text = reader(origin)
            except Exception as exc:  # noqa: BLE001 - keep the original even if a reader fails
                sys.stderr.write(f"warning: sidecar extraction failed for {source}: {exc}\n")
            else:
                extracted = target.with_name(target.name + ".extracted.md")
                extracted.write_text(text, encoding="utf-8")
                sidecar = str(extracted.relative_to(task_root))
        # Never expose qa_type (insight/distractor): it would tell the agent
        # which files matter and invalidate the benchmark.
        rows.append(
            f"| `{destination.as_posix()}` | {item.get('app') or '-'} | "
            f"{f'`{sidecar}`' if sidecar else '-'} |"
        )
    environment = [
        f"# DRBench environment: {task_id}",
        "",
        f"Research question: {metadata.get('question') or case.get('input') or ''}",
        "",
        "Cite the **original path** in the first column exactly as written. The reader sidecar is only "
        "for opening PDF/DOCX/XLSX/PPTX/JSONL content; do not cite the sidecar path.",
        "",
        "| Original path | App | Reader sidecar |",
        "|---|---|---|",
        *rows,
        "",
    ]
    (task_root / "ENVIRONMENT.md").write_text("\n".join(environment), encoding="utf-8")
    return task_root


def _start_run(api: Api, case: dict[str, Any], args: argparse.Namespace) -> str:
    metadata = case.get("metadata") or {}
    task_id = str(metadata.get("task_id") or "")
    body = {
        "conversation_id": None,
        "message": (
            f"{case.get('input')}\n\n"
            f"Start by reading `drbench/{task_id}/ENVIRONMENT.md`. "
            "Return the final report as the required fenced JSON block."
        ),
        "metadata": {
            "eval_case_id": case.get("eval_case_id"),
            "benchmark": "drbench",
            "trigger": "benchmark",
            "drbench_task_id": task_id,
            "drbench_subset": metadata.get("subset"),
            "drbench_domain": metadata.get("domain"),
        },
        "stream": True,
    }
    request = urllib.request.Request(
        f"{api.base_url}/agent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream", "X-API-Key": api.api_key or ""},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                run_id = event.get("run_id")
                if run_id:
                    return str(run_id)
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode("utf-8", errors="replace")) from exc
    raise RuntimeError("agent stream closed without a run_id")


def _wait_run(api: Api, run_id: str, args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.run_timeout_seconds
    while True:
        run = api.get(f"/agent/runs/{_quote(run_id)}")
        if run.get("status") in TERMINAL_RUN_STATUSES:
            return run
        if time.monotonic() >= deadline:
            try:
                api.post(f"/agent/runs/{_quote(run_id)}/cancel", {})
            except Exception:  # noqa: BLE001, S110 - best effort
                pass
            return {"id": run_id, "status": "timeout", "error_code": "agent_timeout"}
        time.sleep(max(1.0, args.poll_seconds))


def _salvage_report(block: str) -> dict[str, Any] | None:
    """Recover a report whose ``report_text`` is not valid JSON.

    Models sometimes paste raw Markdown into ``report_text`` (for example
    ``## What baseline would "15%" apply to?``), which leaves unescaped quotes
    and breaks the whole block even though ``report_insights`` is intact.
    """
    key_index = block.find(_INSIGHTS_KEY)
    if key_index < 0:
        return None
    start = block.find("[", key_index)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(block)):
        char = block[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    insights = json.loads(block[start : index + 1], strict=False)
                except json.JSONDecodeError:
                    return None
                if not isinstance(insights, list):
                    return None
                return {"report_insights": insights, "report_text": _salvage_report_text(block)}
    return None


def _salvage_report_text(block: str) -> str:
    key_index = block.find(_REPORT_TEXT_KEY)
    if key_index < 0:
        return ""
    colon = block.find(":", key_index)
    if colon < 0:
        return ""
    text = block[colon + 1 :].lstrip()
    if text.startswith('"'):
        text = text[1:]
    text = text.rstrip()
    while text and text[-1] in '}"':
        text = text[:-1]
    return text.rstrip()


def extract_report(content: str) -> dict[str, Any] | None:
    for block in reversed(_FENCE_RE.findall(content or "")):
        block = block.strip()
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            parsed = _salvage_report(block)
        if isinstance(parsed, dict) and isinstance(parsed.get("report_insights"), list):
            return parsed
    stripped = (content or "").strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("report_insights"), list):
            return parsed
    return None


def _run_case(api: Api, case: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    metadata = case.get("metadata") or {}
    task_id = str(metadata.get("task_id") or "")
    result: dict[str, Any] = {
        "eval_case_id": case.get("eval_case_id"),
        "task_id": task_id,
        "domain": metadata.get("domain"),
        "run_id": None,
        "run_status": None,
        "tokens": 0,
        "report": None,
        "raw_output": "",
        "failure": None,
    }
    task_root = args.workspace_host / args.workspace_subdir / task_id
    try:
        _materialize(case, data_root=args.data_root, workspace_host=args.workspace_host, subdir=args.workspace_subdir)
        run_id = _start_run(api, case, args)
        result["run_id"] = run_id
        run = _wait_run(api, run_id, args)
        result["run_status"] = run.get("status")
        result["tokens"] = int(run.get("total_tokens") or 0)
        result["raw_output"] = run.get("partial_content") or ""
        if run.get("status") != "completed":
            result["failure"] = f"agent: {run.get('status')} {run.get('error_code') or ''}".strip()
            return result
        report = extract_report(result["raw_output"])
        if report is None:
            result["report"] = {"report_text": result["raw_output"], "report_insights": []}
            result["failure"] = "report: no structured report_insights found"
        else:
            result["report"] = report
        return result
    except Exception as exc:  # noqa: BLE001 - record every per-case failure
        result["failure"] = f"infra: {type(exc).__name__}: {exc}"
        return result
    finally:
        if not args.keep_files and task_root.exists():
            shutil.rmtree(task_root, ignore_errors=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _summarize(results: list[dict[str, Any]], started_at: str) -> dict[str, Any]:
    counts = {"attempted": len(results), "report_extracted": 0, "agent_failed": 0, "report_failed": 0}
    by_domain: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "report": 0})
    for result in results:
        domain = str(result.get("domain") or "unknown")
        by_domain[domain]["n"] += 1
        if result.get("report"):
            counts["report_extracted"] += 1
            by_domain[domain]["report"] += 1
        if (result.get("failure") or "").startswith("agent:"):
            counts["agent_failed"] += 1
        elif result.get("failure"):
            counts["report_failed"] += 1
    return {
        "started_at": started_at,
        "counts": counts,
        "tokens": sum(int(result.get("tokens") or 0) for result in results),
        "by_domain": dict(by_domain),
        "results": results,
    }


def _load_results(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    rows = payload.get("results") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("DRBENCH_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("DRBENCH_API_KEY") or os.environ.get("ADMIN_API_KEY"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--workspace-host", type=Path, required=True)
    parser.add_argument("--workspace-subdir", default="drbench")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--case-ids", default="")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--max-total-tokens", type=int, default=8_000_000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started_at = _now()
    api = Api(args.base_url, args.api_key)
    cases = _load_cases(api, args)
    if not cases:
        sys.stderr.write("error: no drbench-* goldens found\n")
        return 2
    if args.resume:
        previous = _load_results(args.out)
        done = {str(row.get("eval_case_id")) for row in previous if row.get("failure") is None}
        cases = [case for case in cases if case["eval_case_id"] not in done]
        retrying = {case["eval_case_id"] for case in cases}
        results = [row for row in previous if str(row.get("eval_case_id")) not in retrying]
    else:
        results = []
    sys.stdout.write(f"selected {len(cases)} DRBench case(s)\n")
    total_tokens = sum(int(row.get("tokens") or 0) for row in results)
    for index, case in enumerate(cases, start=1):
        if total_tokens >= args.max_total_tokens:
            sys.stdout.write(f"token budget {args.max_total_tokens} reached; stopping\n")
            break
        sys.stdout.write(f"[{index}/{len(cases)}] {case['eval_case_id']} ... ")
        sys.stdout.flush()
        result = _run_case(api, case, args)
        results.append(result)
        total_tokens += int(result.get("tokens") or 0)
        _write_json(args.out, _summarize(results, started_at))
        state = result.get("failure") or "report"
        sys.stdout.write(f"{state} ({result.get('tokens', 0)} tokens)\n")
    report = _summarize(results, started_at)
    _write_json(args.out, report)
    counts = report["counts"]
    sys.stdout.write(
        f"done: reports={counts['report_extracted']}/{counts['attempted']} "
        f"agent_failed={counts['agent_failed']} tokens={report['tokens']}\n"
    )
    sys.stdout.write(f"report: {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
