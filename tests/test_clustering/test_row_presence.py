"""
Tests for clustering/cluster_mismatches.py's detect_row_presence_patterns
and RowPresenceCluster -- the architectural extension for patterns whose
signal shows up as extra/missing rows rather than column-level mismatches.
"""

import pytest

from wherefore.clustering.cluster_mismatches import (
    RowPresenceCluster,
    detect_row_presence_patterns,
)
from wherefore.comparison.diff_engine import compare
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, generate_dataset
from wherefore.synthetic.corruptors.dedup_failure import apply as inject_dedup_failure
from wherefore.synthetic.corruptors.key_mismatch import apply as inject_key_mismatch


def test_real_dedup_failure_fixture_is_detected_with_dataframes():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, new_keys = inject_dedup_failure(source, key_column="account_id", affected_fraction=0.15, seed=1)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=target)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.side == "target_only"
    assert cluster.is_unrecognized is False
    assert cluster.candidate_patterns[0].pattern_id == "dedup_failure"
    assert cluster.candidate_patterns[0].confidence == 1.0


def test_genuinely_new_rows_are_honestly_unrecognized():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=35, seed=1)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=target)

    assert len(clusters) == 1
    assert clusters[0].is_unrecognized is True
    assert clusters[0].candidate_patterns == []


def test_no_dataframes_provided_degrades_to_unrecognized_not_crash():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = inject_dedup_failure(source, key_column="account_id", affected_fraction=0.15, seed=1)
    result = compare(source, target, join_columns="account_id")

    clusters = detect_row_presence_patterns(result)
    assert len(clusters) == 1
    assert clusters[0].is_unrecognized is True


def test_no_unmatched_rows_produces_no_clusters():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=1)
    result = compare(source, source.copy(), join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=source)
    assert clusters == []


def test_source_only_side_is_detected_independently_of_target_only():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, new_keys = inject_dedup_failure(source, key_column="account_id", affected_fraction=0.15, seed=1)
    target = target[target["account_id"] != source.iloc[0]["account_id"]].reset_index(drop=True)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=target)

    sides = {c.side for c in clusters}
    assert sides == {"source_only", "target_only"}

    target_only_cluster = next(c for c in clusters if c.side == "target_only")
    assert target_only_cluster.candidate_patterns[0].pattern_id == "dedup_failure"

    source_only_cluster = next(c for c in clusters if c.side == "source_only")
    assert source_only_cluster.is_unrecognized is True


def test_invalid_confidence_threshold_raises():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=1)
    target, _ = inject_dedup_failure(source, key_column="account_id", seed=1)
    result = compare(source, target, join_columns="account_id")

    with pytest.raises(ValueError, match="confidence_threshold"):
        detect_row_presence_patterns(result, source_df=source, target_df=target, confidence_threshold=1.5)


def test_row_presence_cluster_has_no_narrative_field():
    from wherefore.clustering.cluster_mismatches import RowPresenceMatch

    cluster_fields = set(RowPresenceCluster.__dataclass_fields__)
    match_fields = set(RowPresenceMatch.__dataclass_fields__)
    forbidden_words = {"narrative", "explanation", "cause", "reason"}

    assert not (cluster_fields & forbidden_words)
    assert not (match_fields & forbidden_words)


def test_real_key_mismatch_fixture_is_detected_on_both_sides():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, original_keys = inject_key_mismatch(source, key_column="account_id", affected_fraction=0.2, seed=1)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=target)

    assert len(clusters) == 2
    for cluster in clusters:
        pattern_ids = {p.pattern_id for p in cluster.candidate_patterns}
        assert "key_mismatch" in pattern_ids
        key_mismatch_match = next(p for p in cluster.candidate_patterns if p.pattern_id == "key_mismatch")
        assert key_mismatch_match.confidence == 1.0
        assert key_mismatch_match.signature_name == "key_format_similarity"


def test_key_mismatch_runs_without_source_or_target_dataframes():
    """
    Unlike dedup_failure (which needs the full comparison DataFrame to
    check row VALUE content), key_format_similarity only needs
    diff_result's own unmatched-row keys -- confirmed by direct testing
    that key_mismatch is still detected even when neither
    source_df nor target_df is passed, the one case where dedup_failure
    itself degrades to unrecognized.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = inject_key_mismatch(source, key_column="account_id", affected_fraction=0.2, seed=1)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result)

    assert len(clusters) == 2
    for cluster in clusters:
        pattern_ids = {p.pattern_id for p in cluster.candidate_patterns}
        assert "key_mismatch" in pattern_ids
        assert "dedup_failure" not in pattern_ids  # needs comparison_df, which wasn't passed here


def test_key_mismatch_and_dedup_failure_can_legitimately_co_occur():
    """
    Confirmed by direct testing: a row whose key was merely reformatted
    has, by construction, non-key VALUES identical to its own original
    row -- which duplicate_content_fraction sees as "this row's content
    matches some row elsewhere," the same signal it uses for genuine
    dedup_failure. Both signatures correctly fire on the same
    key_mismatch fixture; this is documented, intentional behavior
    (see cluster_mismatches.py's _detect_row_presence_candidates
    docstring), not a bug to suppress.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = inject_key_mismatch(source, key_column="account_id", affected_fraction=0.2, seed=1)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=target)

    for cluster in clusters:
        pattern_ids = {p.pattern_id for p in cluster.candidate_patterns}
        assert pattern_ids == {"dedup_failure", "key_mismatch"}


def test_dedup_failure_fixture_does_not_spuriously_trigger_key_mismatch():
    """
    Regression guard: dedup_failure's own fixture duplicates a row
    under a NEW, unrelated auto-generated key (e.g. "DUPE-0") -- this
    should NOT also register as key_mismatch, since "DUPE-0" doesn't
    normalize to match any genuinely-unmatched key on the other side.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = inject_dedup_failure(source, key_column="account_id", affected_fraction=0.15, seed=1)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=target)

    assert len(clusters) == 1
    cluster = clusters[0]
    pattern_ids = {p.pattern_id for p in cluster.candidate_patterns}
    assert pattern_ids == {"dedup_failure"}


def test_unrelated_keys_sharing_a_common_prefix_do_not_false_positive_as_key_mismatch():
    """
    Regression guard for the false positive discovered while building
    key_format_similarity: two DIFFERENT datasets (different seeds, so
    genuinely unrelated records) generated from the same domain share
    an ID prefix/format (e.g. "ACCT-1000XX") by construction. Confirmed
    by direct testing that this must NOT register as key_mismatch --
    see signatures.py's key_format_similarity docstring for why a raw
    similarity-score threshold would have failed this exact case.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=2)

    result = compare(source, target, join_columns="account_id")
    clusters = detect_row_presence_patterns(result, source_df=source, target_df=target)

    for cluster in clusters:
        assert cluster.is_unrecognized is True
