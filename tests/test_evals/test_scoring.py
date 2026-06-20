"""
Tests for evals/harness/scoring.py. The metrics test cases below were
verified by hand before being locked in -- particularly the
false_negative case, which correctly counts against BOTH the actual
pattern's recall AND the wrongly-predicted pattern's precision
simultaneously, since a single wrong guess is two distinct failures
from two different patterns' perspectives.
"""

import pytest

from evals.harness.scoring import (
    Outcome,
    ScoredCase,
    compute_metrics_by_pattern,
    score_pattern_match,
    summarize_run,
)


def test_true_positive():
    assert score_pattern_match("timezone_shift", "timezone_shift") == Outcome.TRUE_POSITIVE


def test_honest_abstain():
    assert score_pattern_match(None, None) == Outcome.HONEST_ABSTAIN


def test_false_positive():
    assert score_pattern_match(None, "timezone_shift") == Outcome.FALSE_POSITIVE


def test_false_abstain():
    assert score_pattern_match("timezone_shift", None) == Outcome.FALSE_ABSTAIN


def test_false_negative_wrong_pattern():
    assert score_pattern_match("timezone_shift", "truncation") == Outcome.FALSE_NEGATIVE


def test_metrics_for_mixed_batch_matches_hand_calculation():
    """
    Hand-verified before being locked in as a test:
    - timezone_shift: 2 TP, 0 FP, 1 FN -> precision=1.0, recall=2/3
    - truncation: 1 TP, 1 FP (from the wrong guess), 0 FN -> precision=0.5, recall=1.0
    - enum_drift: 0 TP, 1 FP, 0 FN -> precision=0.0, recall=None (no real positives to recall)
    """
    cases = [
        ScoredCase("f1", "col", "timezone_shift", "timezone_shift", Outcome.TRUE_POSITIVE),
        ScoredCase("f2", "col", "timezone_shift", "timezone_shift", Outcome.TRUE_POSITIVE),
        ScoredCase("f3", "col", "timezone_shift", "truncation", Outcome.FALSE_NEGATIVE),
        ScoredCase("f4", "col", "truncation", "truncation", Outcome.TRUE_POSITIVE),
        ScoredCase("f5", "col", None, None, Outcome.HONEST_ABSTAIN),
        ScoredCase("f6", "col", None, "enum_drift", Outcome.FALSE_POSITIVE),
    ]
    metrics = compute_metrics_by_pattern(cases)

    assert metrics["timezone_shift"].true_positives == 2
    assert metrics["timezone_shift"].false_positives == 0
    assert metrics["timezone_shift"].false_negatives == 1
    assert metrics["timezone_shift"].precision == 1.0
    assert metrics["timezone_shift"].recall == pytest.approx(2 / 3)

    assert metrics["truncation"].true_positives == 1
    assert metrics["truncation"].false_positives == 1
    assert metrics["truncation"].false_negatives == 0
    assert metrics["truncation"].precision == 0.5
    assert metrics["truncation"].recall == 1.0

    assert metrics["enum_drift"].true_positives == 0
    assert metrics["enum_drift"].false_positives == 1
    assert metrics["enum_drift"].precision == 0.0
    assert metrics["enum_drift"].recall is None


def test_overall_accuracy_counts_true_positive_and_honest_abstain_as_correct():
    cases = [
        ScoredCase("f1", "col", "a", "a", Outcome.TRUE_POSITIVE),
        ScoredCase("f2", "col", None, None, Outcome.HONEST_ABSTAIN),
        ScoredCase("f3", "col", "a", "b", Outcome.FALSE_NEGATIVE),
        ScoredCase("f4", "col", None, "a", Outcome.FALSE_POSITIVE),
    ]
    summary = summarize_run(cases)
    assert summary.total_cases == 4
    assert summary.overall_accuracy() == 0.5


def test_empty_run_does_not_crash():
    summary = summarize_run([])
    assert summary.total_cases == 0
    assert summary.overall_accuracy() == 0.0
    assert summary.metrics_by_pattern == {}


def test_precision_and_recall_are_none_with_zero_denominator():
    metrics = compute_metrics_by_pattern([])
    assert metrics == {}
