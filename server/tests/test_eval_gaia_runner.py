"""Regression tests for the host-side GAIA benchmark runner."""

from __future__ import annotations

import json

from scripts import run_gaia_benchmark as runner


def test_resume_replaces_retried_failure_instead_of_double_counting(tmp_path, monkeypatch):
    out = tmp_path / "smoke.json"
    stale = {
        "eval_case_id": "gaia-validation-a",
        "level": 1,
        "attachments": ["none"],
        "failure": "agent: failed agent_upstream_error",
        "gaia_metric": None,
    }
    out.write_text(json.dumps({"results": [stale]}), encoding="utf-8")
    cases = [{"eval_case_id": "gaia-validation-a", "level": 1, "metadata": {}}]
    fresh = {
        "eval_case_id": "gaia-validation-a",
        "level": 1,
        "attachments": ["none"],
        "run_status": "completed",
        "eval_status": "completed",
        "failure": None,
        "gaia_metric": {"score": 1.0, "success": True},
        "tokens": 10,
    }
    monkeypatch.setattr(runner, "_load_cases", lambda api, args: cases)
    monkeypatch.setattr(runner, "_run_case", lambda api, case, args: fresh)

    code = runner.main(
        [
            "--resume",
            "--api-key",
            "test",
            "--data-root",
            str(tmp_path),
            "--workspace-host",
            str(tmp_path),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["counts"]["attempted"] == 1
    assert report["counts"]["scored"] == 1
    assert report["accuracy"]["scored_only"] == 1.0
