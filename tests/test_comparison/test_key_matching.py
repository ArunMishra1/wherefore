"""
Tests for comparison/key_matching.py. Every scenario here was manually
verified against real rapidfuzz scoring behavior before writing
fuzzy_match_keys -- including the discovery that a genuinely different
key can still score 40-50 (not near zero) against an unrelated key,
which is why min_confidence is an explicit, enforced floor rather than
"pick whatever scores highest."
"""

import pandas as pd
import pytest

from wherefore.comparison.key_matching import (
    FuzzyMatchResult,
    content_sanity_check,
    fuzzy_match_keys,
)


def test_exact_matches_score_100_and_are_never_ambiguous():
    result = fuzzy_match_keys(["ACCT-100001"], ["ACCT-100001"])
    assert result.matched_pairs == {"ACCT-100001": "ACCT-100001"}
    assert result.confidence_by_target_key["ACCT-100001"] == 100.0
    assert result.ambiguous_target_keys == []


def test_reformatted_keys_match_with_high_confidence():
    """Dashes stripped during migration -- the realistic case this
    module exists for."""
    source_keys = ["ACCT-100001", "ACCT-100002", "ACCT-100003"]
    target_keys = ["ACCT100001", "ACCT100002", "ACCT100003"]

    result = fuzzy_match_keys(source_keys, target_keys)
    assert result.matched_pairs == {
        "ACCT100001": "ACCT-100001",
        "ACCT100002": "ACCT-100002",
        "ACCT100003": "ACCT-100003",
    }
    for confidence in result.confidence_by_target_key.values():
        assert confidence >= 90  # reformatting consistently scores ~95 in practice


def test_genuinely_different_key_is_not_force_matched():
    """
    Confirmed by direct exploration: a genuinely different key can
    still score ~45 against an unrelated source key -- well above
    zero, but well below the confidence floor. Must land in
    unmatched_target_keys, not be silently matched.
    """
    source_keys = ["ACCT-100001", "ACCT-100002", "ACCT-100003"]
    target_keys = ["ACCT-999999"]

    result = fuzzy_match_keys(source_keys, target_keys)
    assert result.matched_pairs == {}
    assert result.unmatched_target_keys == ["ACCT-999999"]


def test_ambiguous_tie_is_not_auto_matched():
    """
    Confirmed by direct exploration: ACCT-100010 and ACCT-100016 score
    IDENTICALLY against ACCT100013. Silently picking either would be a
    coin flip presented as a confident match.
    """
    source_keys = ["ACCT-100010", "ACCT-100016"]
    target_keys = ["ACCT100013"]

    result = fuzzy_match_keys(source_keys, target_keys)
    assert result.matched_pairs == {}
    assert result.ambiguous_target_keys == ["ACCT100013"]


def test_each_source_key_used_at_most_once():
    source_keys = ["ACCT-100001"]
    target_keys = ["ACCT100001", "ACCT-100001x"]

    result = fuzzy_match_keys(source_keys, target_keys)
    assert len(result.matched_pairs) <= 1
    matched_source_values = list(result.matched_pairs.values())
    assert len(matched_source_values) == len(set(matched_source_values))


def test_exact_match_is_not_stolen_by_competing_fuzzy_match():
    source_keys = ["ACCT-100001", "ACCT-100002"]
    target_keys = ["ACCT-100001", "ACCT100001"]  # one exact, one fuzzy, both could want ACCT-100001

    result = fuzzy_match_keys(source_keys, target_keys)
    assert result.matched_pairs["ACCT-100001"] == "ACCT-100001"
    assert result.confidence_by_target_key["ACCT-100001"] == 100.0


def test_known_limitation_fuzzy_key_may_match_remaining_pool_after_exact_claims_run():
    """
    Documents the known limitation from key_matching.py's docstring:
    once ACCT-100001 is claimed by an exact match, ACCT100001 (fuzzy)
    is left to compare only against ACCT-100002 -- and still clears
    the confidence floor even though it's not really a strong match in
    absolute terms. This is correct given the one-key-once design; this
    test exists so the behavior is documented and intentional, not an
    unnoticed bug.
    """
    source_keys = ["ACCT-100001", "ACCT-100002"]
    target_keys = ["ACCT-100001", "ACCT100001"]

    result = fuzzy_match_keys(source_keys, target_keys)
    assert result.matched_pairs["ACCT100001"] == "ACCT-100002"


def test_empty_source_keys_leaves_all_targets_unmatched():
    result = fuzzy_match_keys([], ["ACCT-100001"])
    assert result.unmatched_target_keys == ["ACCT-100001"]


def test_min_confidence_is_configurable():
    source_keys = ["ACCT-100001"]
    target_keys = ["ACCT-999999"]  # scores ~45 against ACCT-100001

    strict = fuzzy_match_keys(source_keys, target_keys, min_confidence=90.0)
    assert strict.matched_pairs == {}

    lenient = fuzzy_match_keys(source_keys, target_keys, min_confidence=10.0)
    assert "ACCT-999999" in lenient.matched_pairs


def test_result_is_a_plain_dataclass():
    result = fuzzy_match_keys(["a"], ["a"])
    assert isinstance(result, FuzzyMatchResult)


# content_sanity_check: regression coverage for a real false-positive
# found by direct testing (PERFORMANCE.md, Round 8) -- two genuinely
# unrelated records whose keys differ only in separator/case can score
# above DEFAULT_MIN_CONFIDENCE in fuzzy_match_keys, since key strings
# alone carry no information to distinguish "same record, reformatted"
# from "different record, coincidentally similar key." These tests
# also cover a real false-NEGATIVE found while fixing the false
# positive: an overly strict majority check (`> half`) wrongly
# rejected a genuine reformat that also had an independently real
# value mismatch on a 2-voting-column table (1 of 2 matching is a tie,
# not a majority) -- both directions are tested directly, not assumed
# to generalize from only one.

def test_genuine_reformat_with_high_cardinality_match_is_accepted():
    """The realistic case: same record, key reformatted, all other
    columns identical."""
    source_df = pd.DataFrame({
        "id": ["EMP-001"],
        "name": ["Alice"],
        "amount": [100.0],
        "status": ["active"],  # low cardinality, excluded from the vote
    })
    target_df = pd.DataFrame({
        "id": ["EMP001"],
        "name": ["Alice"],
        "amount": [100.0],
        "status": ["active"],
    })
    accepted, rejected = content_sanity_check(
        source_df, target_df, "id", {"EMP001": "EMP-001"}
    )
    assert accepted == {"EMP001": "EMP-001"}
    assert rejected == []


def test_unrelated_records_with_coincidentally_similar_keys_are_rejected():
    """The real false positive this function exists to catch: two
    different employees whose keys differ only by separator type."""
    source_df = pd.DataFrame({
        "id": ["EMP-900000"],
        "name": ["removed_employee"],
        "amount": [1294.42],
        "status": ["active"],
    })
    target_df = pd.DataFrame({
        "id": ["EMP_900000"],
        "name": ["unrelated_new_employee"],
        "amount": [8649.33],
        "status": ["active"],
    })
    accepted, rejected = content_sanity_check(
        source_df, target_df, "id", {"EMP_900000": "EMP-900000"}
    )
    assert accepted == {}
    assert rejected == ["EMP_900000"]


def test_genuine_reformat_with_an_independent_real_value_mismatch_is_still_accepted():
    """The real false negative found while fixing the false positive:
    a row can legitimately have BOTH a key reformat AND an unrelated,
    real value mismatch -- these are independent problems, not
    mutually exclusive. With only 2 high-cardinality voting columns
    (name, amount) and amount genuinely differing, that's exactly 1 of
    2 matching -- a tie, which must still be ACCEPTED (>= half), not
    rejected as if it were the same shape as the false-positive case
    above (0 of 2 matching)."""
    source_df = pd.DataFrame({
        "id": ["EMP-000295"],
        "name": ["name_295"],
        "amount": [9027.5],
        "category": ["alpha"],  # low cardinality, excluded
        "status": ["active"],  # low cardinality, excluded
    })
    target_df = pd.DataFrame({
        "id": ["EMP000295"],
        "name": ["name_295"],
        "amount": [9527.5],  # a real, independent +500 value mismatch
        "category": ["alpha"],
        "status": ["active"],
    })
    accepted, rejected = content_sanity_check(
        source_df, target_df, "id", {"EMP000295": "EMP-000295"}
    )
    assert accepted == {"EMP000295": "EMP-000295"}
    assert rejected == []


def test_exact_key_matches_skip_the_content_check_entirely():
    """An exact key match was never fuzzy -- no key-similarity
    coincidence risk exists, so it should never be rejected by this
    check, even if its content looks completely unrelated (which
    would be a real data bug for the diff to find, not a fuzzy-match
    sanity-check concern)."""
    source_df = pd.DataFrame({"id": ["A"], "name": ["x"], "amount": [1.0]})
    target_df = pd.DataFrame({"id": ["A"], "name": ["totally_different"], "amount": [99999.0]})
    accepted, rejected = content_sanity_check(source_df, target_df, "id", {"A": "A"})
    assert accepted == {"A": "A"}
    assert rejected == []


def test_a_row_with_no_high_cardinality_columns_is_accepted_by_default():
    """Edge case: every non-key column is genuinely low-cardinality,
    even with enough rows to trust that judgment (the row-count
    fallback does not apply here, by design -- 25 rows, all sharing
    one of two status values). There is no content signal available
    to check against, so the match is accepted as-is rather than
    rejected with no real evidence."""
    n = 25
    source_df = pd.DataFrame({
        "id": [f"EMP-{i}" for i in range(n)],
        "status": ["active", "inactive"] * (n // 2) + ["active"],
    })
    target_df = pd.DataFrame({
        "id": [f"EMP{i}" if i == 0 else f"EMP-{i}" for i in range(n)],
        "status": source_df["status"].tolist(),
    })
    accepted, rejected = content_sanity_check(
        source_df, target_df, "id", {"EMP0": "EMP-0"}
    )
    assert accepted == {"EMP0": "EMP-0"}
    assert rejected == []


def test_small_dataframe_does_not_silently_disable_the_cardinality_filter():
    """Real bug found while writing these tests, not a theoretical
    edge case: on a DataFrame with very few rows, EVERY column has
    nunique() <= the threshold purely because there's barely any data
    to be distinct across -- not because the columns are genuinely
    low-cardinality. Confirmed directly: without the row-count
    fallback, this exact false-positive scenario (2 unrelated rows,
    high-cardinality name/amount columns that happen to look
    low-cardinality only because there's just one row each) was
    wrongly ACCEPTED instead of rejected. Below
    MIN_ROWS_TO_TRUST_CARDINALITY, every non-key column must be used
    for voting instead of filtered by cardinality."""
    source_df = pd.DataFrame({
        "id": ["EMP-900000"],
        "name": ["removed_employee"],
        "amount": [1294.42],
    })
    target_df = pd.DataFrame({
        "id": ["EMP_900000"],
        "name": ["unrelated_new_employee"],
        "amount": [8649.33],
    })
    # With only 1 row, name.nunique() == amount.nunique() == 1 --
    # both would be wrongly excluded as "low cardinality" without the
    # small-DataFrame fallback, leaving zero voting columns and a
    # default-accept of a genuinely unrelated pair.
    accepted, rejected = content_sanity_check(
        source_df, target_df, "id", {"EMP_900000": "EMP-900000"}
    )
    assert accepted == {}
    assert rejected == ["EMP_900000"]
