import pytest

from app.eval.goldens import (
    GoldenPayload,
    build_snapshot,
    golden_fields_from_snapshot,
    normalize_snapshot,
    snapshot_hash,
    validate_goldens_file,
)


def _payload(**overrides):
    values = {
        "eval_case_id": "case-1",
        "input": "What is the price?",
        "expected_output": "10 USD",
        "expected_tools": [{"name": "WebSearch"}],
        "context": ["pricing page"],
        "notes": "maintenance note",
    }
    values.update(overrides)
    return GoldenPayload.model_validate(values)


def test_snapshot_hash_survives_key_order_and_reload():
    snapshot = build_snapshot(_payload())
    reordered = {key: snapshot[key] for key in reversed(list(snapshot))}
    assert normalize_snapshot(snapshot) == normalize_snapshot(reordered)
    assert snapshot_hash(snapshot) == snapshot_hash(reordered)


def test_snapshot_excludes_maintenance_notes():
    snapshot = build_snapshot(_payload(notes="changed later"))
    assert "notes" not in snapshot
    assert golden_fields_from_snapshot(snapshot)["input"] == "What is the price?"


def test_snapshot_hash_changes_with_reference_content():
    before = snapshot_hash(build_snapshot(_payload()))
    after = snapshot_hash(build_snapshot(_payload(expected_output="11 USD")))
    assert before != after


def test_goldens_file_requires_an_array():
    with pytest.raises(ValueError):
        validate_goldens_file({"eval_case_id": "case-1"})


def test_goldens_file_rejects_missing_input():
    with pytest.raises(ValueError):
        validate_goldens_file([{"eval_case_id": "case-1"}])
