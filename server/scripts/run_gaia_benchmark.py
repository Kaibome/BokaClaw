"""Run a GAIA validation smoke benchmark through BokaClaw's public HTTP APIs.

This script is intended to run on the host with the standard library only. It
starts one agent run per case, waits for it to finish, submits an evaluation,
and writes a per-level accuracy report.

Example:

    python server/scripts/run_gaia_benchmark.py \
      --base-url http://127.0.0.1:8000 \
      --api-key "$GAIA_API_KEY" \
      --data-root ~/BokaClaw/gaia_data \
      --workspace-host ~/BokaClaw/workspace \
      --limit 20 --levels 1,2,3 --sample stratified \
      --out ~/BokaClaw/gaia_results/smoke.json
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
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_EVAL_STATUSES = {"completed", "failed", "cancelled"}
CASE_PREFIX = "gaia-validation-"
SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ApiError(RuntimeError):
    def __init__(self, status: int, payload: Any, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload
        self.retry_after = retry_after


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_request(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    api_key: str | None = None,
    timeout: float = 60.0,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:  # noqa: BLE001 - malformed error bodies are returned as text
            payload = raw.decode("utf-8", errors="replace")
        retry_after = None
        if exc.headers and exc.headers.get("Retry-After"):
            try:
                retry_after = float(exc.headers["Retry-After"])
            except ValueError:
                retry_after = None
        raise ApiError(exc.code, payload, retry_after) from exc
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


class GaiaApi:
    def __init__(self, base_url: str, api_key: str | None, *, request_interval: float = 0.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.request_interval = max(0.0, request_interval)
        self._last_request = 0.0

    def _throttle(self) -> None:
        if self.request_interval <= 0:
            return
        wait = self.request_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, path: str, *, timeout: float = 60.0) -> Any:
        return self._with_retry("GET", path, None, timeout=timeout)

    def post(self, path: str, body: dict[str, Any], *, timeout: float = 60.0) -> Any:
        return self._with_retry("POST", path, body, timeout=timeout)

    def _with_retry(self, method: str, path: str, body: dict[str, Any] | None, *, timeout: float) -> Any:
        attempts = 0
        while True:
            self._throttle()
            try:
                return _json_request(
                    self._url(path),
                    method=method,
                    body=body,
                    api_key=self.api_key,
                    timeout=timeout,
                )
            except ApiError as exc:
                if exc.status != 429 or attempts >= 3:
                    raise
                attempts += 1
                time.sleep((exc.retry_after or 2.0) * attempts)

    def post_stream_meta(self, path: str, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        self._throttle()
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = urllib.request.Request(self._url(path), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                for event, payload in _iter_sse(response):
                    if event == "error":
                        raise ApiError(502, payload)
                    if event == "meta":
                        return json.loads(payload)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise ApiError(exc.code, raw) from exc
        raise ApiError(502, "SSE stream ended before the meta event")


def _iter_sse(response: Any) -> Iterator[tuple[str, str]]:
    event = "message"
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event, "\n".join(data_lines)


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _metadata(case: dict[str, Any]) -> dict[str, Any]:
    metadata = case.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _level(case: dict[str, Any]) -> int:
    try:
        return int(_metadata(case).get("level"))
    except (TypeError, ValueError):
        return 0


def _select_cases(
    cases: list[dict[str, Any]],
    *,
    levels: set[int] | None,
    case_ids: set[str] | None,
    limit: int | None,
    sample: str,
) -> list[dict[str, Any]]:
    selected = []
    for case in cases:
        case_id = str(case.get("eval_case_id") or "")
        task_id = str(_metadata(case).get("task_id") or "")
        if not case_id.startswith(CASE_PREFIX):
            continue
        if case_ids is not None and case_id not in case_ids and task_id not in case_ids:
            continue
        if levels is not None and _level(case) not in levels:
            continue
        selected.append(case)
    selected.sort(key=lambda case: (_level(case), str(case.get("eval_case_id"))))
    if limit is None or limit >= len(selected):
        return selected
    if sample == "first":
        return selected[:limit]
    groups: dict[int, list[dict[str, Any]]] = {}
    for case in selected:
        groups.setdefault(_level(case), []).append(case)
    picked: list[dict[str, Any]] = []
    while len(picked) < limit:
        advanced = False
        for level in sorted(groups):
            if len(picked) >= limit:
                break
            if groups[level]:
                picked.append(groups[level].pop(0))
                advanced = True
        if not advanced:
            break
    return picked


def _load_cases(api: GaiaApi, args: argparse.Namespace) -> list[dict[str, Any]]:
    items = api.get("/evals/goldens?limit=500")
    cases = [item for item in items or [] if str(item.get("eval_case_id", "")).startswith(CASE_PREFIX)]
    levels = {int(part) for part in args.levels.split(",") if part.strip()} if args.levels else None
    case_ids = {part.strip() for part in args.case_ids.split(",") if part.strip()} if args.case_ids else None
    selected = _select_cases(cases, levels=levels, case_ids=case_ids, limit=args.limit, sample=args.sample)
    return [api.get(f"/evals/goldens/{_quote(case['eval_case_id'])}") for case in selected]


def _attachment_source(data_root: Path, attachment: dict[str, Any]) -> Path | None:
    raw = str(attachment.get("source_path") or "")
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        return None
    source = data_root / relative
    if source.is_file():
        return source
    name = str(attachment.get("name") or "")
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        return None
    for candidate in data_root.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def _materialize(case: dict[str, Any], *, data_root: Path, workspace_host: Path, subdir: str) -> list[str]:
    metadata = _metadata(case)
    task_id = str(metadata.get("task_id") or "")
    if not SAFE_TASK_ID.fullmatch(task_id):
        raise ValueError(f"unsafe GAIA task id: {task_id!r}")
    copied: list[str] = []
    for attachment in metadata.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        source = _attachment_source(data_root, attachment)
        if source is None:
            raise FileNotFoundError(f"attachment not found: {attachment.get('source_path')!r}")
        name = str(attachment.get("name") or source.name)
        workspace_path = str(attachment.get("workspace_path") or f"{subdir}/{task_id}/{name}")
        relative = Path(workspace_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe workspace path: {workspace_path!r}")
        destination = workspace_host / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(str(destination))
    return copied


def _cleanup(workspace_host: Path, subdir: str, task_id: str) -> None:
    target = workspace_host / subdir / task_id
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def _start_run(api: GaiaApi, case: dict[str, Any], args: argparse.Namespace) -> str:
    metadata = _metadata(case)
    body = {
        "conversation_id": None,
        "message": case["input"],
        "stream": True,
        "metadata": {
            "eval_case_id": case["eval_case_id"],
            "benchmark": "gaia",
            "trigger": "benchmark",
            "gaia_task_id": metadata.get("task_id"),
            "gaia_level": metadata.get("level"),
            "gaia_split": metadata.get("split"),
        },
    }
    if args.blocking:
        body["stream"] = False
        try:
            response = api.post("/agent", body, timeout=args.run_timeout_seconds + 60)
            return str(response["run_id"])
        except ApiError as exc:
            payload = exc.payload if isinstance(exc.payload, dict) else {}
            detail = payload.get("detail") if isinstance(payload, dict) else None
            run_id = detail.get("run_id") if isinstance(detail, dict) else payload.get("run_id")
            if exc.status == 504 and run_id:
                return str(run_id)
            raise
    meta = api.post_stream_meta(
        "/agent",
        body,
        timeout=min(60.0, max(10.0, args.run_timeout_seconds)),
    )
    run_id = meta.get("run_id")
    if not run_id:
        raise ApiError(502, f"agent meta event has no run_id: {meta}")
    return str(run_id)


def _wait_run(api: GaiaApi, run_id: str, args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.run_timeout_seconds
    while True:
        run = api.get(f"/agent/runs/{_quote(run_id)}")
        if run.get("status") in TERMINAL_RUN_STATUSES:
            return run
        if time.monotonic() >= deadline:
            try:
                api.post(f"/agent/runs/{_quote(run_id)}/cancel", {})
            except Exception:  # noqa: BLE001, S110 - cancellation is best effort
                pass
            return {"id": run_id, "status": "timeout", "error_code": "agent_timeout"}
        time.sleep(max(0.5, args.poll_seconds))


def _submit_eval(api: GaiaApi, run_id: str, case_id: str, judge: bool) -> dict[str, Any]:
    try:
        return api.post(
            "/evals",
            {"run_id": run_id, "eval_case_id": case_id, "use_llm_judge": judge},
            timeout=60,
        )
    except ApiError as exc:
        if exc.status != 409:
            raise
        rows = api.get(
            f"/evals?run_id={_quote(run_id)}&eval_case_id={_quote(case_id)}&limit=20"
        )
        for row in rows or []:
            if row.get("status") in {"queued", "running", "completed"}:
                return row
        raise


def _wait_eval(api: GaiaApi, eval_id: str, args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + args.eval_timeout_seconds
    while True:
        row = api.get(f"/evals/{_quote(eval_id)}")
        if row.get("status") in TERMINAL_EVAL_STATUSES:
            return row
        if time.monotonic() >= deadline:
            return {"id": eval_id, "status": "timeout", "error_code": "eval_timeout"}
        time.sleep(max(0.5, args.poll_seconds))


def _gaia_metric(eval_row: dict[str, Any]) -> dict[str, Any] | None:
    for metric in eval_row.get("metrics") or []:
        if metric.get("name") == "GAIA Exact Match":
            return metric
    return None


def _run_case(api: GaiaApi, case: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    metadata = _metadata(case)
    task_id = str(metadata.get("task_id") or "")
    attachment_kinds = [str(item.get("kind")) for item in metadata.get("attachments") or [] if isinstance(item, dict)]
    result: dict[str, Any] = {
        "eval_case_id": case.get("eval_case_id"),
        "task_id": task_id,
        "level": _level(case),
        "attachments": attachment_kinds or ["none"],
        "run_id": None,
        "eval_id": None,
        "run_status": None,
        "eval_status": None,
        "gaia_metric": None,
        "tokens": 0,
        "failure": None,
    }
    try:
        _materialize(case, data_root=args.data_root, workspace_host=args.workspace_host, subdir=args.workspace_subdir)
    except Exception as exc:  # noqa: BLE001 - record every per-case failure
        result["failure"] = f"attachment: {type(exc).__name__}: {exc}"
        if not args.keep_files:
            _cleanup(args.workspace_host, args.workspace_subdir, task_id)
        return result

    try:
        run_id = _start_run(api, case, args)
        result["run_id"] = run_id
        run = _wait_run(api, run_id, args)
        result["run_status"] = run.get("status")
        result["tokens"] = int(run.get("total_tokens") or 0)
        if run.get("status") != "completed":
            result["failure"] = f"agent: {run.get('status')} {run.get('error_code') or ''}".strip()
            return result

        eval_row = _submit_eval(api, run_id, str(case["eval_case_id"]), args.judge)
        result["eval_id"] = eval_row.get("id")
        eval_row = _wait_eval(api, str(eval_row["id"]), args)
        result["eval_status"] = eval_row.get("status")
        if eval_row.get("status") != "completed":
            result["failure"] = f"eval: {eval_row.get('status')} {eval_row.get('error_code') or ''}".strip()
            return result
        result["gaia_metric"] = _gaia_metric(eval_row)
        if result["gaia_metric"] is None:
            result["failure"] = "eval: GAIA Exact Match metric missing"
        return result
    except Exception as exc:  # noqa: BLE001 - record every per-case failure
        result["failure"] = f"infra: {type(exc).__name__}: {exc}"
        return result
    finally:
        if not args.keep_files:
            _cleanup(args.workspace_host, args.workspace_subdir, task_id)


def _load_results(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a corrupt resume file means start over
        return []
    results = payload.get("results") if isinstance(payload, dict) else None
    return results if isinstance(results, list) else []


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _summarize(results: list[dict[str, Any]], *, started_at: str, args: argparse.Namespace) -> dict[str, Any]:
    counts = {"attempted": len(results), "scored": 0, "agent_failed": 0, "eval_failed": 0, "other_failed": 0}
    correct = 0
    by_level: dict[str, dict[str, int]] = {}
    by_kind: dict[str, dict[str, int]] = {}
    failures: dict[str, int] = {}
    for result in results:
        level = str(result.get("level") or 0)
        kinds = result.get("attachments") or ["none"]
        level_bucket = by_level.setdefault(level, {"n": 0, "correct": 0})
        level_bucket["n"] += 1
        for kind in set(kinds):
            kind_bucket = by_kind.setdefault(kind, {"n": 0, "correct": 0})
            kind_bucket["n"] += 1
        metric = result.get("gaia_metric")
        if metric is not None and result.get("eval_status") == "completed":
            counts["scored"] += 1
            if metric.get("score") == 1.0:
                correct += 1
                level_bucket["correct"] += 1
                for kind in set(kinds):
                    by_kind[kind]["correct"] += 1
        elif result.get("failure", "").startswith("agent:"):
            counts["agent_failed"] += 1
        elif result.get("failure", "").startswith("eval:"):
            counts["eval_failed"] += 1
        else:
            counts["other_failed"] += 1
        if result.get("failure"):
            key = result["failure"].split(":", 1)[0]
            failures[key] = failures.get(key, 0) + 1
    for bucket in list(by_level.values()) + list(by_kind.values()):
        bucket["accuracy"] = round(bucket["correct"] / bucket["n"], 4) if bucket["n"] else 0.0
    return {
        "benchmark": "gaia",
        "split": "validation",
        "scorer": "gaia_exact_match_v1",
        "started_at": started_at,
        "finished_at": _now(),
        "counts": counts,
        "accuracy": {
            "all_attempted": round(correct / len(results), 4) if results else 0.0,
            "scored_only": round(correct / counts["scored"], 4) if counts["scored"] else 0.0,
            "by_level": by_level,
            "by_attachment_kind": by_kind,
        },
        "failures": failures,
        "options": {
            "limit": args.limit,
            "levels": args.levels,
            "sample": args.sample,
            "judge": args.judge,
        },
        "results": results,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("GAIA_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("GAIA_API_KEY") or os.environ.get("ADMIN_API_KEY"))
    parser.add_argument("--data-root", type=Path, default=Path(os.environ.get("GAIA_DATA_HOST", "gaia_data")))
    parser.add_argument(
        "--workspace-host",
        type=Path,
        default=Path(os.environ.get("GAIA_WORKSPACE_HOST", "workspace")),
    )
    parser.add_argument("--workspace-subdir", default="gaia")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--levels", default="1,2,3")
    parser.add_argument("--sample", choices=("stratified", "first"), default="stratified")
    parser.add_argument("--case-ids", default="")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--blocking", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--request-interval", type=float, default=0.0)
    parser.add_argument("--run-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--eval-timeout-seconds", type=float, default=960.0)
    parser.add_argument("--max-total-tokens", type=int, default=2_000_000)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(os.environ.get("GAIA_RESULTS_DIR", "gaia_results")) / "smoke.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started_at = _now()
    api = GaiaApi(args.base_url, args.api_key, request_interval=args.request_interval)
    try:
        cases = _load_cases(api, args)
    except Exception as exc:  # noqa: BLE001 - CLI error report
        sys.stderr.write(f"error: failed to load GAIA cases: {exc}\n")
        return 2
    if not cases:
        sys.stderr.write("error: no gaia-validation-* goldens found\n")
        return 2

    if args.resume:
        previous = _load_results(args.out)
        done = {str(row.get("eval_case_id")) for row in previous if row.get("failure") is None}
        cases = [case for case in cases if case["eval_case_id"] not in done]
        # Drop the old rows for cases we are retrying; otherwise a failed case
        # would be counted twice (once as the stale failure, once as the retry).
        retrying = {case["eval_case_id"] for case in cases}
        results = [row for row in previous if str(row.get("eval_case_id")) not in retrying]
    else:
        results = []

    sys.stdout.write(f"selected {len(cases)} case(s)\n")
    if args.dry_run:
        for case in cases:
            kinds = [item.get("kind") for item in _metadata(case).get("attachments") or []]
            sys.stdout.write(f"{case['eval_case_id']} level={_level(case)} attachments={kinds or ['none']}\n")
        return 0

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
        _write_json(args.out, _summarize(results, started_at=started_at, args=args))
        metric = result.get("gaia_metric") or {}
        state = "correct" if metric.get("score") == 1.0 else (result.get("failure") or "incorrect")
        sys.stdout.write(f"{state}\n")

    report = _summarize(results, started_at=started_at, args=args)
    _write_json(args.out, report)
    counts = report["counts"]
    accuracy = report["accuracy"]
    sys.stdout.write(
        f"done: scored={counts['scored']}/{counts['attempted']} "
        f"accuracy(scored)={accuracy['scored_only']:.3f} "
        f"accuracy(all)={accuracy['all_attempted']:.3f}\n"
    )
    for level in sorted(accuracy["by_level"]):
        bucket = accuracy["by_level"][level]
        sys.stdout.write(f"  L{level}: {bucket['correct']}/{bucket['n']} = {bucket['accuracy']:.3f}\n")
    if report["failures"]:
        sys.stdout.write(f"failures: {report['failures']}\n")
    sys.stdout.write(f"report: {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
