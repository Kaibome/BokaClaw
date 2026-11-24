"""Tests for the DRBench importer and report extraction helpers."""

from __future__ import annotations

import json

import pytest
from scripts.import_drbench_goldens import build_golden
from scripts.run_drbench_benchmark import _safe_destination, extract_report


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_build_golden_maps_task_and_env_files(tmp_path):
    task_root = tmp_path / "data" / "tasks" / "DR0001"
    _write(
        task_root / "config" / "task.json",
        {
            "dr_question": "How can Lee's Market improve compliance?",
            "company_info": {"name": "Lee's Market", "description": "Regional grocer."},
            "persona": {"name": "Emily Patel", "role": "Regulatory Affairs Manager", "domain": "compliance"},
        },
    )
    _write(
        task_root / "config" / "env.json",
        {
            "env_files": [
                {
                    "source": "drbench/data/tasks/DR0001/files/IN001_pdf/report.pdf",
                    "destination": "shared/report.pdf",
                    "app": "nextcloud",
                    "qa_type": "insight",
                }
            ]
        },
    )
    _write(task_root / "config" / "eval.json", {"dr_report_evaluation_qa": []})

    golden = build_golden(tmp_path, "minival", "DR0001")

    assert golden.eval_case_id == "drbench-minival-DR0001"
    assert golden.metadata["benchmark"] == "drbench"
    assert golden.metadata["task_id"] == "DR0001"
    assert golden.metadata["question"].startswith("How can Lee's Market")
    assert golden.metadata["env_files"][0]["source"] == "data/tasks/DR0001/files/IN001_pdf/report.pdf"
    assert golden.metadata["eval_hash"]
    assert "ENVIRONMENT.md" in golden.input


def test_extract_report_prefers_a_fenced_json_block():
    content = (
        "Research notes...\n\n"
        "```json\n"
        '{"report_insights":[{"claim":"A","citations":["shared/a.pdf"]}],"report_text":"Body"}\n'
        "```\n"
    )

    report = extract_report(content)

    assert report is not None
    assert report["report_insights"][0]["claim"] == "A"
    assert report["report_text"] == "Body"


def test_extract_report_rejects_plain_text():
    assert extract_report("A free-form report without a JSON block") is None


def test_safe_destination_rejects_traversal_add_absolute_paths():
    with pytest.raises(ValueError):
        _safe_destination("../escape.pdf")
    with pytest.raises(ValueError):
        _safe_destination("/etc/passwd")
    assert _safe_destination("shared/report.pdf").as_posix() == "shared/report.pdf"
