from app.eval.gaia import (
    answers_match,
    extract_final_answer,
    gaia_metric_row,
    normalize_answer,
    record_to_golden,
    select_gaia_records,
)
from app.eval.goldens import GoldenPayload, build_snapshot


def _row(task_id: str, level: int, *, file_name: str | None = None) -> dict:
    return {
        "task_id": task_id,
        "Question": f"Question {task_id}",
        "Level": level,
        "Final answer": f"answer-{task_id}",
        "file_name": file_name or "",
        "file_path": f"2023/validation/{file_name}" if file_name else "",
    }


def test_extract_final_answer_handles_markers_and_fallbacks():
    assert extract_final_answer("work\nFinal answer: 42") == "42"
    assert extract_final_answer("**Final answer:** Paris.") == "Paris"
    assert extract_final_answer("答案：北京") == "北京"
    assert extract_final_answer("no marker here") == "no marker here"
    assert extract_final_answer("") is None


def test_normalize_answer_handles_case_articles_and_numbers():
    assert normalize_answer("The Eiffel Tower.") == "eiffel tower"
    assert normalize_answer("1,000") == "1000"
    assert normalize_answer("$50%") == "50"
    assert normalize_answer("  Multiple   spaces  ") == "multiple spaces"


def test_answers_match_is_quasi_exact():
    assert answers_match("1,000", "1000")
    assert answers_match("The Moon", "moon")
    assert answers_match("3.0", "3")
    assert answers_match("Paris.", "paris")
    assert not answers_match("42", "43")
    assert not answers_match("", "42")


def test_gaia_metric_row_shape_and_details():
    row = gaia_metric_row("work\nFinal answer: 42", "42")
    assert row["name"] == "GAIA Exact Match"
    assert row["score"] == 1.0
    assert row["success"] is True
    assert row["details"]["extracted_answer"] == "42"

    wrong = gaia_metric_row("Final answer: 41", "42")
    assert wrong["score"] == 0.0
    assert wrong["success"] is False

    missing = gaia_metric_row("anything", None)
    assert missing["skipped"] is True
    assert missing["success"] is None


def test_select_gaia_records_filters_unsupported_and_samples_by_level():
    rows = [
        _row("l1-text", 1),
        _row("l1-image", 1, file_name="chart.png"),
        _row("l2-pdf", 2, file_name="doc.pdf"),
        _row("l3-sheet", 3, file_name="sheet.xlsx"),
    ]

    default = select_gaia_records(rows, limit=2, sample="stratified")
    assert [record.task_id for record in default.records] == ["l1-text", "l2-pdf"]
    assert default.skipped_unsupported == 2

    included = select_gaia_records(rows, include_unsupported=True)
    assert {record.task_id for record in included.records} == {
        "l1-text",
        "l1-image",
        "l2-pdf",
        "l3-sheet",
    }


def test_record_to_golden_carries_metadata_and_answer_instruction():
    selection = select_gaia_records([_row("task-1", 1, file_name="doc.pdf")])
    golden = record_to_golden(selection.records[0])

    assert golden.eval_case_id == "gaia-validation-task-1"
    assert golden.expected_output == "answer-task-1"
    assert "Final answer:" in golden.input
    assert "gaia/task-1/doc.pdf" in golden.input
    assert golden.metadata["benchmark"] == "gaia"
    assert golden.metadata["attachments"][0]["kind"] == "pdf"


def test_snapshot_includes_metadata_only_when_present():
    without = GoldenPayload(eval_case_id="case-1", input="hello")
    assert "metadata" not in build_snapshot(without)

    with_metadata = GoldenPayload(
        eval_case_id="gaia-validation-task-1",
        input="hello",
        metadata={"benchmark": "gaia", "level": 1},
    )
    snapshot = build_snapshot(with_metadata)
    assert snapshot["metadata"] == {"benchmark": "gaia", "level": 1}
