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
    constant_offset_subset,
    get_signature,
)
from wherefore.comparison.diff_engine import compare
from wherefore.comparison.diff_result import MismatchRow
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, generate_dataset
from wherefore.synthetic.corruptors.timezone_shift import apply


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
