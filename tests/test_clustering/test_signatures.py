"""
Tests for clustering/signatures.py. Each case here was manually
verified against real and synthetic mismatch data before being locked
in as a test -- see project history for the exploration that grounded
the constant_offset_subset design (tolerant of minority noise, not
requiring a literal 100% match).
"""

import pandas as pd
import pytest

from wherefore.clustering.signatures import (
    SIGNATURE_REGISTRY,
    consistent_value_mapping,
    constant_offset_subset,
    get_signature,
    truncated_prefix,
)
from wherefore.comparison.diff_engine import compare
from wherefore.comparison.diff_result import MismatchRow
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, HEALTHCARE_PATIENTS, generate_dataset
from wherefore.synthetic.corruptors.enum_drift import apply as drift
from wherefore.synthetic.corruptors.timezone_shift import apply
from wherefore.synthetic.corruptors.truncation import apply as truncate


def _make_mismatch(key_val, source, target):
    return MismatchRow(key={"id": key_val}, column="val", source_value=source, target_value=target)


def test_real_timezone_shift_fixture_scores_full_confidence():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=42)
    target, _ = apply(source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1)
    result = compare(source, target, join_columns="account_id")

    confidence = constant_offset_subset(result.mismatches_for_column("opened_at"))
    assert confidence == 1.0


def test_random_unrelated_deltas_score_low():
    base = pd.Timestamp("2024-01-01")
    mismatches = [_make_mismatch(i, base, base + pd.Timedelta(hours=i)) for i in range(1, 11)]
    confidence = constant_offset_subset(mismatches)
    assert confidence == pytest.approx(0.1)


def test_majority_shared_delta_with_minority_noise():
    base = pd.Timestamp("2024-01-01")
    majority = [_make_mismatch(i, base, base + pd.Timedelta(hours=5)) for i in range(7)]
    noise = [_make_mismatch(100 + i, base, base + pd.Timedelta(hours=100 + i)) for i in range(3)]
    confidence = constant_offset_subset(majority + noise)
    assert confidence == pytest.approx(0.7)


def test_empty_cluster_returns_zero_not_error():
    assert constant_offset_subset([]) == 0.0


def test_zero_delta_does_not_count_as_a_shift():
    base = pd.Timestamp("2024-01-01")
    # Every "mismatch" has source == target -- shouldn't happen in
    # practice (mismatches list implies inequality), but the signature
    # should not report false confidence on a degenerate zero-delta case.
    mismatches = [_make_mismatch(i, base, base) for i in range(5)]
    assert constant_offset_subset(mismatches) == 0.0


def test_non_subtractable_values_excluded_not_crashed():
    mismatches = [
        _make_mismatch(1, "abc", "def"),  # strings: not subtractable
        _make_mismatch(2, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-01") + pd.Timedelta(hours=5)),
        _make_mismatch(3, pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-02") + pd.Timedelta(hours=5)),
    ]
    # Should not raise; the 2 subtractable rows share a delta, the
    # unsubtractable one is excluded from the denominator entirely.
    confidence = constant_offset_subset(mismatches)
    assert confidence == 1.0


def test_get_signature_returns_registered_function():
    fn = get_signature("constant_offset_subset")
    assert fn is constant_offset_subset


def test_get_signature_raises_on_unknown_name():
    with pytest.raises(KeyError, match="Unknown signature"):
        get_signature("not_a_real_signature")


def test_signature_registry_contains_constant_offset_subset():
    assert "constant_offset_subset" in SIGNATURE_REGISTRY


def test_real_truncation_fixture_scores_full_confidence():
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    target, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.3, seed=1)
    result = compare(source, target, join_columns="patient_id")

    confidence = truncated_prefix(result.mismatches_for_column("patient_name"))
    assert confidence == 1.0


def test_genuinely_different_strings_score_zero():
    mismatches = [
        _make_mismatch(1, "Alice Smith", "Bob Jones"),
        _make_mismatch(2, "Carol White", "Dave Black"),
    ]
    assert truncated_prefix(mismatches) == 0.0


def test_target_longer_than_source_does_not_count_as_truncation():
    """
    A target value LONGER than source (e.g. a suffix appended, or an
    unrelated value that happens to start the same way) is a different
    failure mode entirely -- must not be mistaken for truncation.
    """
    mismatches = [_make_mismatch(1, "Alice", "Alice Smith")]
    assert truncated_prefix(mismatches) == 0.0


def test_truncated_prefix_does_not_require_uniform_cut_length():
    """
    Unlike constant_offset_subset, truncation doesn't require every
    row to be cut to the SAME length -- different source strings can
    be cut to different resulting lengths under a shared byte/character
    limit. The signature should still score these as truncation.
    """
    mismatches = [
        _make_mismatch(1, "Alexandria Johnson", "Alexandri"),  # cut to 9 chars
        _make_mismatch(2, "Bo", "B"),  # cut to 1 char
    ]
    assert truncated_prefix(mismatches) == 1.0


def test_signature_registry_contains_truncated_prefix():
    assert "truncated_prefix" in SIGNATURE_REGISTRY


def test_real_enum_drift_fixture_scores_full_confidence():
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target, _ = drift(source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=1)
    result = compare(source, target, join_columns="patient_id")

    confidence = consistent_value_mapping(result.mismatches_for_column("claim_status"))
    assert confidence == 1.0


def test_truncation_fixture_does_not_false_positive_on_consistent_value_mapping():
    """
    Regression test for a real bug caught during development:
    consistent_value_mapping originally scored 1.0 on ANY cluster where
    every distinct source value appeared exactly once (vacuously
    "consistent with itself"), including a pure truncation fixture
    where every name is unique. This meant truncation and enum_drift
    would BOTH match the same real truncation cluster -- a genuine
    cross-contamination risk once two string-dtype patterns compete
    for the same clusters. Fixed by requiring at least one source value
    to repeat before counting as evidence; see signatures.py docstring.
    """
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    target, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.5, seed=1)
    result = compare(source, target, join_columns="patient_id")

    confidence = consistent_value_mapping(result.mismatches_for_column("patient_name"))
    assert confidence == 0.0


def test_inconsistent_mapping_scores_low():
    mismatches = [
        _make_mismatch(1, "approved", "APPROVED"),
        _make_mismatch(2, "approved", "Approved"),
        _make_mismatch(3, "approved", "OK"),
    ]
    confidence = consistent_value_mapping(mismatches)
    assert confidence == pytest.approx(1 / 3)


def test_mixed_clean_and_inconsistent_mapping_scores_partial():
    mismatches = [
        _make_mismatch(1, "approved", "APPROVED"),
        _make_mismatch(2, "approved", "APPROVED"),
        _make_mismatch(3, "denied", "REJECTED"),
        _make_mismatch(4, "denied", "DENIED"),
    ]
    assert consistent_value_mapping(mismatches) == pytest.approx(0.75)


def test_single_occurrence_per_source_value_scores_zero_not_one():
    """
    Direct check of the fix: a cluster where no source value repeats
    contributes zero confidence, not the vacuous 1.0 it scored before
    the fix.
    """
    mismatches = [
        _make_mismatch(1, "alpha", "ALPHA"),
        _make_mismatch(2, "beta", "BETA"),
        _make_mismatch(3, "gamma", "GAMMA"),
    ]
    assert consistent_value_mapping(mismatches) == 0.0


def test_empty_cluster_returns_zero_for_consistent_value_mapping():
    assert consistent_value_mapping([]) == 0.0


def test_signature_registry_contains_consistent_value_mapping():
    assert "consistent_value_mapping" in SIGNATURE_REGISTRY
