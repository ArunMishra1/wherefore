"""
Tests for comparison/key_matching.py. Every scenario here was manually
verified against real rapidfuzz scoring behavior before writing
fuzzy_match_keys -- including the discovery that a genuinely different
key can still score 40-50 (not near zero) against an unrelated key,
which is why min_confidence is an explicit, enforced floor rather than
"pick whatever scores highest."
"""

import pytest

from wherefore.comparison.key_matching import FuzzyMatchResult, fuzzy_match_keys


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
