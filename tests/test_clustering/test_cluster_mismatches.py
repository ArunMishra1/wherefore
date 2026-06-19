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
from wherefore.synthetic.corruptors.timezone_shift import apply


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
    account_type has no taxonomy pattern targeting string/enum columns
    yet (enum_drift isn't built -- see TAXONOMY_TODO.md). Clustering
    must report this as unrecognized, not force-fit an unrelated pattern.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=42)
    target = source.copy()
    target.loc[0:5, "account_type"] = "unknown_type"

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
