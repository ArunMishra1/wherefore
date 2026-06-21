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
    float32_precision_drift,
    get_signature,
    mojibake_reversible,
    null_sentinel_coercion,
    truncated_prefix,
)
from wherefore.comparison.diff_engine import compare
from wherefore.comparison.diff_result import MismatchRow
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, HEALTHCARE_PATIENTS, generate_dataset
from wherefore.synthetic.corruptors.encoding_mismatch import apply as drift_encoding
from wherefore.synthetic.corruptors.enum_drift import apply as drift
from wherefore.synthetic.corruptors.float_precision import apply as drift_float
from wherefore.synthetic.corruptors.null_type_coercion import apply as coerce_null
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


def test_real_null_type_coercion_fixture_scores_full_confidence():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=1)
    target, _ = coerce_null(
        source, column="last_transaction_at", sentinel="NULL", affected_fraction=1.0, seed=1
    )
    result = compare(source, target, join_columns="account_id")
    confidence = null_sentinel_coercion(result.mismatches_for_column("last_transaction_at"))
    assert confidence == 1.0


def test_unrelated_mismatch_scores_zero_for_null_sentinel_coercion():
    mismatches = [_make_mismatch(1, "2024-01-01", "2024-01-02")]
    assert null_sentinel_coercion(mismatches) == 0.0


def test_both_sides_null_different_representation_is_not_evidence():
    """
    NaN vs NaT are both genuinely null, just different pandas null
    representations -- this is NOT a type-coercion bug (nothing was
    coerced to a sentinel STRING), so it must not count as evidence.
    """
    mismatches = [MismatchRow(key={"id": 1}, column="x", source_value=float("nan"), target_value=pd.NaT)]
    assert null_sentinel_coercion(mismatches) == 0.0


def test_reverse_direction_source_sentinel_target_null():
    mismatches = [MismatchRow(key={"id": 1}, column="x", source_value="N/A", target_value=None)]
    assert null_sentinel_coercion(mismatches) == 1.0


def test_does_not_false_positive_on_truncation_or_enum_drift_fixtures():
    """
    Cross-contamination check, same discipline as the truncation/
    enum_drift collision caught earlier in the project: confirms
    null_sentinel_coercion doesn't fire on the other string-targeting
    patterns' real fixtures.
    """
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)

    target1, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.5, seed=1)
    result1 = compare(source, target1, join_columns="patient_id")
    assert null_sentinel_coercion(result1.mismatches_for_column("patient_name")) == 0.0

    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target2, _ = drift(source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=1)
    result2 = compare(source, target2, join_columns="patient_id")
    assert null_sentinel_coercion(result2.mismatches_for_column("claim_status")) == 0.0


def test_case_insensitive_sentinel_matching():
    mismatches = [MismatchRow(key={"id": 1}, column="x", source_value=None, target_value="null")]
    assert null_sentinel_coercion(mismatches) == 1.0


def test_empty_cluster_returns_zero_for_null_sentinel_coercion():
    assert null_sentinel_coercion([]) == 0.0


def test_signature_registry_contains_null_sentinel_coercion():
    assert "null_sentinel_coercion" in SIGNATURE_REGISTRY


def test_real_float_precision_fixture_scores_full_confidence():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = drift_float(source, column="balance", affected_fraction=0.5, seed=1)
    result = compare(source, target, join_columns="account_id")
    confidence = float32_precision_drift(result.mismatches_for_column("balance"))
    assert confidence == 1.0


def test_cents_rounding_bug_on_large_value_does_not_false_positive():
    """
    THE regression test for a real bug caught while building this
    signature: a magnitude-based heuristic (relative delta below a
    threshold) was tried first and scored this case as 0.5 confidence
    -- a one-cent rounding bug on a six-figure value (98762.17 ->
    98762.18) has a relative magnitude small enough to coincidentally
    look like float32 noise, even though 98762.17 actually rounds to
    98762.171875 in float32, not 98762.18. The fix checks the EXACT
    float32 round-trip instead of approximating its size, which
    correctly rejects this case.
    """
    mismatches = [
        _make_mismatch(1, 98762.17, 98762.18),
        _make_mismatch(2, 500.00, 500.01),
    ]
    assert float32_precision_drift(mismatches) == 0.0


def test_unrelated_large_change_scores_zero():
    mismatches = [_make_mismatch(1, 100.0, 999.0)]
    assert float32_precision_drift(mismatches) == 0.0


def test_non_numeric_values_excluded_not_crashed():
    mismatches = [_make_mismatch(1, "abc", "def")]
    assert float32_precision_drift(mismatches) == 0.0


def test_does_not_false_positive_on_other_string_or_datetime_fixtures():
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)

    target1, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.5, seed=1)
    result1 = compare(source, target1, join_columns="patient_id")
    assert float32_precision_drift(result1.mismatches_for_column("patient_name")) == 0.0

    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target2, _ = drift(source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=1)
    result2 = compare(source, target2, join_columns="patient_id")
    assert float32_precision_drift(result2.mismatches_for_column("claim_status")) == 0.0


def test_does_not_false_positive_on_null_type_coercion_fixture_same_float_column():
    """
    Cross-contamination check specific to sharing the same dtype
    family: both null_type_coercion and float_precision target float
    columns, so confirm a null-coerced-to-sentinel fixture on a float
    column doesn't also trigger float32_precision_drift.
    """
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=1)
    target, _ = coerce_null(source, column="billed_amount", sentinel="NULL", affected_fraction=1.0, seed=1)
    result = compare(source, target, join_columns="patient_id")
    assert float32_precision_drift(result.mismatches_for_column("billed_amount")) == 0.0


def test_empty_cluster_returns_zero_for_float32_precision_drift():
    assert float32_precision_drift([]) == 0.0


def test_signature_registry_contains_float32_precision_drift():
    assert "float32_precision_drift" in SIGNATURE_REGISTRY


def test_real_encoding_mismatch_fixture_scores_full_confidence():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=1)
    target, _ = drift_encoding(source, column="customer_name", affected_fraction=1.0, seed=1)
    result = compare(source, target, join_columns="account_id")
    confidence = mojibake_reversible(result.mismatches_for_column("customer_name"))
    assert confidence == 1.0


def test_known_mojibake_example_is_detected():
    mismatches = [_make_mismatch(1, "José", "JosÃ©")]
    assert mojibake_reversible(mismatches) == 1.0


def test_unrelated_string_change_scores_zero():
    mismatches = [_make_mismatch(1, "approved", "denied")]
    assert mojibake_reversible(mismatches) == 0.0


def test_truncated_value_does_not_false_positive_as_mojibake():
    mismatches = [_make_mismatch(1, "Susan Miller", "Susan Mi")]
    assert mojibake_reversible(mismatches) == 0.0


def test_does_not_false_positive_on_truncation_or_enum_drift_fixtures():
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)

    target1, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.5, seed=1)
    result1 = compare(source, target1, join_columns="patient_id")
    assert mojibake_reversible(result1.mismatches_for_column("patient_name")) == 0.0

    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target2, _ = drift(source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=1)
    result2 = compare(source, target2, join_columns="patient_id")
    assert mojibake_reversible(result2.mismatches_for_column("claim_status")) == 0.0


def test_known_partial_overlap_with_consistent_value_mapping_stays_below_threshold():
    """
    Documents a real, confirmed partial overlap: a real
    encoding_mismatch fixture scores ~0.33 on consistent_value_mapping
    (not zero), since the synthetic name generator occasionally
    repeats the same first name across different last names, and a
    repeated source value consistently mapping to the same mojibake
    target is, technically, also a "consistent value mapping" for
    that subset. Same kind of legitimate low-level overlap already
    documented for null_type_coercion/enum_drift -- correctly stays
    well below the 0.9 confidence threshold.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=1)
    target, _ = drift_encoding(source, column="customer_name", affected_fraction=1.0, seed=1)
    result = compare(source, target, join_columns="account_id")
    mismatches = result.mismatches_for_column("customer_name")

    confidence = consistent_value_mapping(mismatches)
    assert confidence < 0.9
    assert confidence > 0.0


def test_empty_cluster_returns_zero_for_mojibake_reversible():
    assert mojibake_reversible([]) == 0.0


def test_signature_registry_contains_mojibake_reversible():
    assert "mojibake_reversible" in SIGNATURE_REGISTRY


def test_duplicate_content_fraction_detects_real_duplicates():
    from wherefore.clustering.signatures import duplicate_content_fraction
    from wherefore.comparison.diff_result import RowPresenceRecord

    comparison_df = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"], "val": [10, 20, 30]})
    unmatched_rows = [
        RowPresenceRecord(key={"id": 99}, values={"name": "a", "val": 10}),  # genuine duplicate of id=1's content
    ]
    confidence = duplicate_content_fraction(unmatched_rows, comparison_df, join_columns=["id"])
    assert confidence == 1.0


def test_duplicate_content_fraction_rejects_genuinely_new_rows():
    from wherefore.clustering.signatures import duplicate_content_fraction
    from wherefore.comparison.diff_result import RowPresenceRecord

    comparison_df = pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"], "val": [10, 20, 30]})
    unmatched_rows = [
        RowPresenceRecord(key={"id": 99}, values={"name": "totally_new", "val": 999}),
    ]
    confidence = duplicate_content_fraction(unmatched_rows, comparison_df, join_columns=["id"])
    assert confidence == 0.0


def test_duplicate_content_fraction_handles_mixed_real_and_new_rows():
    from wherefore.clustering.signatures import duplicate_content_fraction
    from wherefore.comparison.diff_result import RowPresenceRecord

    comparison_df = pd.DataFrame({"id": [1, 2], "name": ["a", "b"], "val": [10, 20]})
    unmatched_rows = [
        RowPresenceRecord(key={"id": 98}, values={"name": "a", "val": 10}),  # duplicate
        RowPresenceRecord(key={"id": 99}, values={"name": "new", "val": 999}),  # genuinely new
    ]
    confidence = duplicate_content_fraction(unmatched_rows, comparison_df, join_columns=["id"])
    assert confidence == 0.5


def test_duplicate_content_fraction_returns_zero_for_empty_inputs():
    from wherefore.clustering.signatures import duplicate_content_fraction

    assert duplicate_content_fraction([], pd.DataFrame({"id": [1]}), ["id"]) == 0.0
    assert duplicate_content_fraction([object()], None, ["id"]) == 0.0


def test_key_format_similarity_detects_real_reformatted_key():
    from wherefore.clustering.signatures import key_format_similarity
    from wherefore.comparison.diff_result import RowPresenceRecord

    unmatched_rows = [RowPresenceRecord(key={"id": "ACCT100003"}, values={})]
    comparison_unmatched_rows = [RowPresenceRecord(key={"id": "ACCT-100003"}, values={})]

    confidence = key_format_similarity(unmatched_rows, comparison_unmatched_rows, join_columns=["id"])
    assert confidence == 1.0


def test_key_format_similarity_detects_case_only_drift():
    from wherefore.clustering.signatures import key_format_similarity
    from wherefore.comparison.diff_result import RowPresenceRecord

    unmatched_rows = [RowPresenceRecord(key={"id": "emp-1001"}, values={})]
    comparison_unmatched_rows = [RowPresenceRecord(key={"id": "EMP-1001"}, values={})]

    confidence = key_format_similarity(unmatched_rows, comparison_unmatched_rows, join_columns=["id"])
    assert confidence == 1.0


def test_key_format_similarity_rejects_genuinely_new_rows():
    from wherefore.clustering.signatures import key_format_similarity
    from wherefore.comparison.diff_result import RowPresenceRecord

    unmatched_rows = [RowPresenceRecord(key={"id": "ACCT-100099"}, values={})]
    comparison_unmatched_rows = [RowPresenceRecord(key={"id": "PT-500003"}, values={})]

    confidence = key_format_similarity(unmatched_rows, comparison_unmatched_rows, join_columns=["id"])
    assert confidence == 0.0


def test_key_format_similarity_does_not_false_positive_on_shared_prefix_unrelated_keys():
    """
    Confirmed by direct testing: a raw rapidfuzz fuzz.ratio threshold
    (the first approach tried) scores two genuinely UNRELATED keys
    sharing a domain's common ID prefix (e.g. "ACCT-100002" vs
    "ACCT-100022" -- different accounts, not a reformat of one
    another) at ~91, well above key_matching.py's 75 accept floor and
    not reliably distinguishable from a genuine reformat's ~93-95
    score. This is exactly the false positive that motivated switching
    to a deterministic normalization-equality check, which has no such
    risk: these two keys normalize to different strings, so they
    correctly score 0.0 here.
    """
    from wherefore.clustering.signatures import key_format_similarity
    from wherefore.comparison.diff_result import RowPresenceRecord

    unmatched_rows = [RowPresenceRecord(key={"id": "ACCT-100002"}, values={})]
    comparison_unmatched_rows = [RowPresenceRecord(key={"id": "ACCT-100022"}, values={})]

    confidence = key_format_similarity(unmatched_rows, comparison_unmatched_rows, join_columns=["id"])
    assert confidence == 0.0


def test_key_format_similarity_handles_mixed_real_and_new_rows():
    from wherefore.clustering.signatures import key_format_similarity
    from wherefore.comparison.diff_result import RowPresenceRecord

    unmatched_rows = [
        RowPresenceRecord(key={"id": "ACCT100001"}, values={}),  # reformatted, has a partner
        RowPresenceRecord(key={"id": "ACCT-999999"}, values={}),  # genuinely new, no partner
    ]
    comparison_unmatched_rows = [RowPresenceRecord(key={"id": "ACCT-100001"}, values={})]

    confidence = key_format_similarity(unmatched_rows, comparison_unmatched_rows, join_columns=["id"])
    assert confidence == 0.5


def test_key_format_similarity_returns_zero_for_empty_inputs():
    from wherefore.clustering.signatures import key_format_similarity
    from wherefore.comparison.diff_result import RowPresenceRecord

    assert key_format_similarity([], [RowPresenceRecord(key={"id": "A-1"}, values={})], ["id"]) == 0.0
    assert key_format_similarity([RowPresenceRecord(key={"id": "A-1"}, values={})], [], ["id"]) == 0.0
