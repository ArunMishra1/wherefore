"""
Tests for clustering/cluster_mismatches.py. Includes a regression test
for a real bug caught while building this: patterns_by_dtype originally
did exact string matching ('datetime64[s]' != 'datetime'), which meant
NO cluster ever matched any pattern on real pandas data, despite the
signature itself scoring correctly in isolation. Fixed in
taxonomy/registry.py via dtype-family matching -- see that module's
_dtype_matches_family for the real dtype strings this was tested against.
"""

import pandas as pd
import pytest

from wherefore.clustering.cluster_mismatches import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    Cluster,
    PatternMatch,
    cluster_mismatches,
)
from wherefore.comparison.diff_engine import compare
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, HEALTHCARE_PATIENTS, generate_dataset
from wherefore.synthetic.corruptors.encoding_mismatch import apply as drift_encoding
from wherefore.synthetic.corruptors.enum_drift import apply as drift
from wherefore.synthetic.corruptors.float_precision import apply as drift_float
from wherefore.synthetic.corruptors.null_type_coercion import apply as coerce_null
from wherefore.synthetic.corruptors.timezone_shift import apply
from wherefore.synthetic.corruptors.truncation import apply as truncate


@pytest.fixture
def timezone_shift_diff_result():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=42)
    target, _ = apply(source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1)
    return compare(source, target, join_columns="account_id")


def test_real_timezone_shift_fixture_is_correctly_identified_end_to_end(timezone_shift_diff_result):
    """
    The regression test for the dtype-family bug: this exact scenario
    previously returned is_unrecognized=True for every cluster, despite
    the underlying signature scoring 1.0 confidence -- the bug was in
    dtype string matching between column_summary and the YAML's
    declared dtype families, not in the signature logic itself.
    """
    clusters = cluster_mismatches(timezone_shift_diff_result)
    assert len(clusters) == 1

    cluster = clusters[0]
    assert cluster.column == "opened_at"
    assert cluster.is_unrecognized is False
    assert len(cluster.candidate_patterns) == 1

    match = cluster.candidate_patterns[0]
    assert match.pattern_id == "timezone_shift"
    assert match.signature_name == "constant_offset_subset"
    assert match.confidence == 1.0


def test_works_on_healthcare_domain_too():
    """Confirms clustering is genuinely domain-agnostic, matching the
    same property already proven for the corruptor and diff engine."""
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    target, _ = apply(source, column="encounter_date", offset_hours=9.0, affected_fraction=0.25, seed=3)
    result = compare(source, target, join_columns="patient_id")

    clusters = cluster_mismatches(result)
    assert len(clusters) == 1
    assert clusters[0].candidate_patterns[0].pattern_id == "timezone_shift"


def test_no_mismatches_produces_no_clusters():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=1)
    result = compare(source, source.copy(), join_columns="account_id")
    assert cluster_mismatches(result) == []


def test_column_with_no_matching_pattern_is_honestly_unrecognized():
    """
    Genuinely random, inconsistent corruption -- each row gets an
    unrelated garbage value, with no consistent source->target mapping
    and no prefix relationship -- must not match any known pattern,
    including enum_drift and truncation (both of which target string
    columns and are otherwise candidates here). Clustering must report
    this as unrecognized, not force-fit either one.

    NOTE: this test originally corrupted every selected row to the SAME
    constant value ("unknown_type") -- but once enum_drift existed,
    that's no longer a valid "nothing matches" case: many rows mapping
    consistently from one source value to one target value IS exactly
    the enum_drift signature, correctly matched. Updated to use
    genuinely inconsistent corruption (different, unrelated garbage
    values per row) so this test still proves what it claims to prove.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=42)
    target = source.copy()
    target.loc[0, "account_type"] = "xq7z"
    target.loc[1, "account_type"] = "random_garbage_2"
    target.loc[2, "account_type"] = "totally_different_value"

    result = compare(source, target, join_columns="account_id")
    clusters = cluster_mismatches(result)

    account_type_cluster = next(c for c in clusters if c.column == "account_type")
    assert account_type_cluster.is_unrecognized is True
    assert account_type_cluster.candidate_patterns == []


def test_confidence_threshold_is_configurable_not_domain_aware(timezone_shift_diff_result):
    """
    Per project decision: clustering itself has no concept of domain.
    Callers control strictness via confidence_threshold directly --
    e.g. an eval harness scoring controlled synthetic fixtures might
    pass 1.0, while a default CLI run uses the more tolerant default.
    """
    strict = cluster_mismatches(timezone_shift_diff_result, confidence_threshold=1.0)
    assert strict[0].candidate_patterns[0].confidence == 1.0  # this fixture is clean, still matches at 1.0

    default = cluster_mismatches(timezone_shift_diff_result)
    assert default[0].candidate_patterns == strict[0].candidate_patterns


def test_invalid_confidence_threshold_raises(timezone_shift_diff_result):
    with pytest.raises(ValueError, match="confidence_threshold"):
        cluster_mismatches(timezone_shift_diff_result, confidence_threshold=1.5)
    with pytest.raises(ValueError, match="confidence_threshold"):
        cluster_mismatches(timezone_shift_diff_result, confidence_threshold=-0.1)


def test_default_threshold_constant_matches_documented_value():
    assert DEFAULT_CONFIDENCE_THRESHOLD == 0.9


def test_cluster_and_pattern_match_are_plain_dataclasses_no_narrative_field():
    """
    Structural guard for the "clustering never makes causal claims"
    constraint: Cluster and PatternMatch should carry only statistical
    facts (column, mismatches, pattern_id, signature_name, confidence)
    -- no narrative/explanation/cause field should ever be added here,
    since that's the reasoning layer's job. This test exists to catch
    a future accidental violation of that boundary.
    """
    cluster_fields = {f for f in Cluster.__dataclass_fields__}
    match_fields = {f for f in PatternMatch.__dataclass_fields__}
    forbidden_words = {"narrative", "explanation", "cause", "reason"}

    assert not (cluster_fields & forbidden_words)
    assert not (match_fields & forbidden_words)


def test_two_independent_corruptions_are_correctly_distinguished_by_column():
    """
    Proves clustering scales beyond a single pattern: two genuinely
    different corruptions (a timezone shift on one column, a
    truncation on another) applied to the same dataset must each be
    correctly identified on their own column, with zero
    cross-contamination -- the truncation cluster must not match
    timezone_shift, and vice versa.
    """
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    target, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.3, seed=1)
    target, _ = apply(target, column="encounter_date", offset_hours=9.0, affected_fraction=0.3, seed=2)

    result = compare(source, target, join_columns="patient_id")
    clusters = cluster_mismatches(result)
    clusters_by_column = {c.column: c for c in clusters}

    assert clusters_by_column["patient_name"].candidate_patterns[0].pattern_id == "truncation"
    assert clusters_by_column["encounter_date"].candidate_patterns[0].pattern_id == "timezone_shift"


def test_three_independent_corruptions_each_match_exactly_one_pattern():
    """
    The real test of cross-contamination risk: enum_drift and
    truncation BOTH target string-dtype columns, so they're both
    candidates for any string mismatch cluster. Before the
    consistent_value_mapping false-positive fix, a pure truncation
    cluster would have matched BOTH truncation AND enum_drift
    simultaneously. With three independent corruptions across three
    columns in one dataset, each column's cluster must match exactly
    its own pattern -- not the other string-dtype pattern too.
    """
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    target, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.5, seed=1)
    target, _ = apply(target, column="encounter_date", offset_hours=9.0, affected_fraction=0.3, seed=2)
    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target, _ = drift(target, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=3)

    result = compare(source, target, join_columns="patient_id")
    clusters = cluster_mismatches(result)
    clusters_by_column = {c.column: c for c in clusters}

    assert [p.pattern_id for p in clusters_by_column["patient_name"].candidate_patterns] == ["truncation"]
    assert [p.pattern_id for p in clusters_by_column["encounter_date"].candidate_patterns] == ["timezone_shift"]
    assert [p.pattern_id for p in clusters_by_column["claim_status"].candidate_patterns] == ["enum_drift"]


def test_null_coerced_to_sentinel_legitimately_matches_two_patterns_by_design():
    """
    Documents and locks in an intentional design decision (see
    cluster_mismatches.py's "On multiple legitimate matches" docstring
    section): a genuine null coerced to a literal sentinel string (the
    null_type_coercion pattern) ALSO satisfies consistent_value_mapping
    (the same source value -- NaT -- consistently maps to the same
    target value -- "NULL"). Clustering does NOT suppress either
    candidate or prioritize one as "more specific" -- that would be a
    causal judgment call clustering is designed not to make. Both
    candidates are reported; disambiguation is the reasoning layer's
    job, which has the actual cited values to reason from.

    If this test ever needs to change to assert ONLY null_type_coercion
    is reported, that's a deliberate architecture change (adding
    priority/suppression logic to clustering) and should be made
    consciously, not as an incidental side effect of an unrelated fix.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=1)
    target, affected = coerce_null(
        source, column="last_transaction_at", sentinel="NULL", affected_fraction=1.0, seed=1
    )
    result = compare(source, target, join_columns="account_id")
    clusters = cluster_mismatches(result)

    cluster = next(c for c in clusters if c.column == "last_transaction_at")
    matched_ids = {p.pattern_id for p in cluster.candidate_patterns}
    assert matched_ids == {"null_type_coercion", "enum_drift"}


def test_float_precision_matches_cleanly_alongside_other_corruptions():
    """
    float_precision and null_type_coercion both target float-dtype
    columns, so confirm a real float_precision corruption on one
    column produces exactly its own pattern, with no cross-talk, even
    in a dataset that also has unrelated corruptions on other columns.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = drift_float(source, column="balance", affected_fraction=0.5, seed=1)
    target, _ = apply(target, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=2)

    result = compare(source, target, join_columns="account_id")
    clusters = cluster_mismatches(result)
    clusters_by_column = {c.column: c for c in clusters}

    assert [p.pattern_id for p in clusters_by_column["balance"].candidate_patterns] == ["float_precision"]
    assert [p.pattern_id for p in clusters_by_column["opened_at"].candidate_patterns] == ["timezone_shift"]


def test_encoding_mismatch_matches_cleanly_alongside_other_corruptions():
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=50, seed=1)
    target, _ = drift_encoding(source, column="patient_name", affected_fraction=1.0, seed=1)
    target, _ = apply(target, column="encounter_date", offset_hours=9.0, affected_fraction=0.3, seed=2)

    result = compare(source, target, join_columns="patient_id")
    clusters = cluster_mismatches(result)
    clusters_by_column = {c.column: c for c in clusters}

    assert [p.pattern_id for p in clusters_by_column["patient_name"].candidate_patterns] == ["encoding_mismatch"]
    assert [p.pattern_id for p in clusters_by_column["encounter_date"].candidate_patterns] == ["timezone_shift"]
