"""Unit tests for the repeated-question finder's pure helpers."""

from __future__ import annotations

from scripts.find_reformulated_questions import (
    Thresholds,
    classify_relation,
    normalize_text,
    numbers,
    trigram_scores,
)


def test_normalize_text_folds_case_width_and_punctuation():
    assert normalize_text(" 2026 国际金融报告！") == "2026国际金融报告"
    assert normalize_text("ＡＢＣ，Report.") == "abcreport"


def test_numbers_extracts_integer_and_decimal_values():
    assert numbers("2026 年营收 12.5 亿，增长 3%") == {"2026", "12.5", "3"}


def test_trigram_containment_detects_an_extended_question():
    first = normalize_text("2026国际金融报告")
    later = normalize_text("2026国际金融报告，重点看风险部分")
    jaccard, containment = trigram_scores(first, later)

    assert containment > 0.9
    assert 0 < jaccard < containment


def test_exact_repeat_wins_over_other_signals():
    first = normalize_text("2026 国际金融报告")
    later = normalize_text("2026国际金融报告。")

    assert classify_relation(
        first, later, containment=1.0, jaccard=1.0, sequence=1.0, cosine=1.0, thresholds=Thresholds()
    ) == "exact"


def test_extended_question_is_detected_from_substring():
    first = normalize_text("2026国际金融报告")
    later = normalize_text("2026国际金融报告，重点看风险部分")

    assert classify_relation(
        first, later, containment=0.9, jaccard=0.4, sequence=0.6, cosine=0.7, thresholds=Thresholds()
    ) == "extended"


def test_vector_similarity_labels_a_paraphrase():
    first = normalize_text("2026国际金融行业分析报告")
    later = normalize_text("分析一下2026年国际金融行业报告")

    assert classify_relation(
        first, later, containment=0.5, jaccard=0.3, sequence=0.5, cosine=0.91, thresholds=Thresholds()
    ) == "paraphrase"


def test_changed_numbers_are_a_modification_not_a_paraphrase():
    first = normalize_text("2025年营收")
    later = normalize_text("2026年营收")

    assert classify_relation(
        first, later, containment=0.6, jaccard=0.5, sequence=0.8, cosine=0.95, thresholds=Thresholds()
    ) == "modified"


def test_unrelated_questions_are_filtered_out():
    first = normalize_text("2026国际金融报告")
    later = normalize_text("帮我写一个 Python 排序算法")

    assert classify_relation(
        first, later, containment=0.1, jaccard=0.1, sequence=0.1, cosine=0.2, thresholds=Thresholds()
    ) == "different"
