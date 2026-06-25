"""
comparison/key_matching.py

Resolves join keys between source and target when they don't align
exactly -- e.g. "CUST-001" vs "CUST001" after a reformatting step
during migration.

Design grounded in real rapidfuzz behavior, checked before writing
this module:
  - Reformatted-but-same keys (dashes stripped, etc.) score ~90-95
    with fuzz.ratio.
  - A genuinely DIFFERENT key (different account entirely) can still
    score in the 40s-50s purely from shared characters/length -- not
    near zero. This means "pick whatever scores highest" is not
    sufficient; a real gap exists between "same record, reformatted"
    and "different record" in practice, but it must be enforced with
    an explicit confidence floor, not assumed.
  - Genuinely ambiguous cases exist: two different source keys can
    score IDENTICALLY against one target key. Silently picking one
    would be a coin-flip presented as a confident match -- this module
    detects exact or near ties among top candidates and refuses to
    auto-match them, surfacing the ambiguity instead.

Fuzzy matches are themselves a taxonomy signal (key_mismatch /
fuzzy-join-issues pattern, not yet built) -- so every match's
confidence is reported, not discarded after use, via
FuzzyMatchResult.confidence_by_target_key.

REAL FALSE-POSITIVE FOUND BY DIRECT TESTING (see PERFORMANCE.md, Round
8): two GENUINELY UNRELATED records whose keys differ only in
separator type or case (e.g. "EMP-900000" vs "EMP_900000" as two real,
different employees who happen to share digits) can score above
DEFAULT_MIN_CONFIDENCE purely from the shared-characters effect this
module's own docstring already warned about in the abstract -- now
confirmed concretely: a constructed test merged 50 such unrelated
pairs and then reported their natural content differences as
fabricated value mismatches, including a false enum_drift match. Key
strings alone cannot distinguish "same record, reformatted" from "two
different records that happen to look similar" -- there is no
information in the key text itself to tell those apart. The fix
(content_sanity_check below) requires looking at the actual row
content, which this module's pure key-matching functions deliberately
do not have access to by design (kept here as logic, applied by the
caller once it has both DataFrames -- see cli.py's
_apply_fuzzy_key_resolution).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz, process

DEFAULT_MIN_CONFIDENCE = 75.0
DEFAULT_AMBIGUITY_GAP = 5.0
LOW_CARDINALITY_THRESHOLD = 5
MIN_ROWS_TO_TRUST_CARDINALITY = 20


@dataclass
class FuzzyMatchResult:
    """
    Result of fuzzy-matching target keys against source keys.

    matched_pairs: target_key -> source_key, for confident, unambiguous
        matches only.
    unmatched_target_keys: target keys that found no source candidate
        above min_confidence -- these should be treated as genuinely
        new/unmatched, not force-matched to the nearest (but still
        low-confidence) candidate.
    ambiguous_target_keys: target keys where the top two source
        candidates scored within `ambiguity_gap` of each other --
        too close to call automatically. Reported separately from
        unmatched so a caller (or future UI) can show the person the
        candidates and let them decide, rather than silently guessing.
    confidence_by_target_key: every confidently-matched target key's
        match score, 0-100 -- preserved because low-but-accepted
        confidence is itself a signal the key_mismatch taxonomy
        pattern will want later, not just an internal detail to discard.
    """

    matched_pairs: dict[str, str] = field(default_factory=dict)
    unmatched_target_keys: list[str] = field(default_factory=list)
    ambiguous_target_keys: list[str] = field(default_factory=list)
    confidence_by_target_key: dict[str, float] = field(default_factory=dict)


def fuzzy_match_keys(
    source_keys: list[str],
    target_keys: list[str],
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ambiguity_gap: float = DEFAULT_AMBIGUITY_GAP,
) -> FuzzyMatchResult:
    """
    For each target key, finds the best-matching source key using
    rapidfuzz's ratio scorer (0-100). A target key is confidently
    matched only if:
      1. its best candidate scores >= min_confidence, AND
      2. its best candidate beats the second-best by more than
         ambiguity_gap (otherwise the match is ambiguous, not confident)

    Already-exact matches (a target key that equals a source key
    verbatim) always match at confidence 100 and are never flagged
    ambiguous, regardless of other source keys' scores -- an exact
    string match is definitionally unambiguous.

    Each source key is used at most once: once matched, it's removed
    from the candidate pool for subsequent target keys, preventing one
    source key from being claimed as the "best match" for multiple
    target keys simultaneously.

    KNOWN LIMITATION, confirmed by direct testing: once a source key is
    claimed (especially by an earlier exact match), a later fuzzy key
    can end up confidently matched to whatever's LEFT in the pool, even
    if that remaining candidate isn't a great match in absolute terms
    -- because the ambiguity check only compares against what's still
    available, not the full original candidate set. This is correct
    given the one-key-once design (it prevents double-claiming), but
    means min_confidence does more of the safety work than
    ambiguity_gap in this scenario. Future mitigation: process target
    keys in descending order of their best score first, so the most
    confident matches claim source keys before weaker matches are
    forced to compete for what's left.
    """
    result = FuzzyMatchResult()
    available_source_keys = list(source_keys)

    # Exact matches first and removed from the pool immediately --
    # this also means an exact match is never "stolen" by a
    # subsequent close-fuzzy-match competing for the same source key.
    remaining_target_keys = []
    for tk in target_keys:
        if tk in available_source_keys:
            result.matched_pairs[tk] = tk
            result.confidence_by_target_key[tk] = 100.0
            available_source_keys.remove(tk)
        else:
            remaining_target_keys.append(tk)

    for tk in remaining_target_keys:
        if not available_source_keys:
            result.unmatched_target_keys.append(tk)
            continue

        top_matches = process.extract(
            tk, available_source_keys, scorer=fuzz.ratio, limit=2
        )

        if not top_matches:
            result.unmatched_target_keys.append(tk)
            continue

        best_key, best_score, _ = top_matches[0]

        if best_score < min_confidence:
            result.unmatched_target_keys.append(tk)
            continue

        if len(top_matches) > 1:
            _, second_score, _ = top_matches[1]
            if (best_score - second_score) < ambiguity_gap:
                result.ambiguous_target_keys.append(tk)
                continue

        result.matched_pairs[tk] = best_key
        result.confidence_by_target_key[tk] = best_score
        available_source_keys.remove(best_key)

    return result


def content_sanity_check(
    source_df: pd.DataFrame,
    target_df: pd.DataFrame,
    join_column: str,
    matched_pairs: dict[str, str],
    low_cardinality_threshold: int = LOW_CARDINALITY_THRESHOLD,
) -> tuple[dict[str, str], list[str]]:
    """
    Filters a fuzzy_match_keys() result against the rows' actual
    content, rejecting matches where the matched source/target rows
    don't plausibly look like the same underlying record despite their
    keys scoring well together. Confirmed by direct testing (see
    key_matching.py's module docstring and PERFORMANCE.md Round 8)
    that key-string similarity alone is not sufficient: two genuinely
    different records can share enough characters to score above
    DEFAULT_MIN_CONFIDENCE, and fuzzy_match_keys has no way to know
    this, since it only ever sees key strings, never row content.

    For each proposed match, compares every non-key column between the
    matched source row and target row, REQUIRING EXACT EQUALITY per
    column (not a tolerance) -- a genuine reformat is the same record
    with at most a small number of independently-corrupted columns
    (the very value-mismatches this tool exists to find), so most
    columns should match exactly; an unrelated coincidental key match
    should look like random chance across columns instead. A majority
    of compared columns matching is required to accept the match.

    Columns with low cardinality (<= low_cardinality_threshold distinct
    values in source_df) are EXCLUDED from the comparison -- confirmed
    by direct testing that a low-cardinality column (e.g. a binary
    status flag) matches by pure chance often enough to make the
    majority vote unreliable; requiring agreement specifically on
    higher-cardinality columns (names, amounts, anything closer to
    unique) makes an accidental majority far less likely than chance
    alone would predict. Deliberately uses EXACT equality even for
    numeric columns, not a tolerance band -- a tolerance introduces
    exactly the kind of untunable, no-clean-line threshold
    key_format_similarity's own docstring already rejected once for a
    similar reason; exact-or-not has no such gradient to mistune.

    REAL BUG FOUND AND FIXED while writing this function's own unit
    tests, not a theoretical edge case: nunique() is computed against
    source_df AS GIVEN -- on a small DataFrame (confirmed directly: as
    few as a handful of rows), EVERY column can have nunique() <=
    low_cardinality_threshold purely because there are barely any rows
    to be distinct across, not because the column is genuinely
    low-cardinality. That would silently disable the cardinality
    filter exactly when there's the least data to be confident from.
    Below MIN_ROWS_TO_TRUST_CARDINALITY rows, cardinality is not
    trusted at all and every non-key column is used for voting instead
    of filtering down to the high-cardinality ones -- a real fallback
    confirmed against an actual small DataFrame, not assumed safe.

    Uses >= (not strict >) against half the voting-column count --
    confirmed by direct testing this matters in practice: a genuine
    reformatted-key row that ALSO has a real, independently-injected
    value mismatch (the realistic case of two unrelated problems on
    the same row -- a key reformat and a data-quality bug are not
    mutually exclusive) can have as few as 2 voting columns with
    exactly 1 matching. A strict majority (> half) wrongly rejects
    that as a tie; >= half correctly accepts it while still rejecting
    the Round 8 false-positive case (0 of 2 voting columns matching),
    confirmed directly against both real cases this function exists
    to distinguish, not assumed to generalize from one alone.

    If a row has zero columns above the cardinality threshold (an
    edge case: every non-key column is low-cardinality even with
    enough rows to trust that judgment), the check is skipped for that
    row and the match is accepted as-is -- there is no content signal
    available to check against in that case, and silently rejecting
    every match on a low-cardinality-only table would be worse than
    the risk this function exists to catch.

    REAL TENSION FOUND BY AN EXISTING TEST BREAKING, not invented:
    with EXACTLY ONE voting column, a real, independently-injected
    value mismatch on that single column is indistinguishable from a
    genuinely unrelated record -- there is no second column left to
    show the row is otherwise the same. Confirmed directly: this broke
    test_fuzzy_keys_flag_resolves_reformatted_keys, an existing,
    legitimate test whose own scenario is exactly this (a 2-row
    comparison, one column besides the key, and a deliberate value
    mismatch on the reformatted row). The check is skipped (the match
    accepted) when there is only one voting column, for the same
    reason it's skipped at zero: not enough signal to safely
    distinguish "real corruption on a real reformat" from "unrelated
    record" in either direction, so this function defers rather than
    guess wrong.

    Returns (filtered_matched_pairs, rejected_target_keys) -- pairs
    that failed the content check move from matched_pairs into a
    rejected list, the same outcome as if fuzzy_match_keys had judged
    them unmatched in the first place. Rejected keys should be treated
    as genuinely unmatched, exactly like fuzzy_match_keys.unmatched_target_keys.
    """
    if join_column not in source_df.columns or join_column not in target_df.columns:
        raise ValueError(f"join_column {join_column!r} not found in both DataFrames")

    non_key_columns = [c for c in source_df.columns if c != join_column and c in target_df.columns]
    if len(source_df) < MIN_ROWS_TO_TRUST_CARDINALITY:
        voting_columns = non_key_columns
    else:
        voting_columns = [
            c for c in non_key_columns
            if source_df[c].nunique(dropna=True) > low_cardinality_threshold
        ]

    source_indexed = source_df.set_index(source_df[join_column].astype(str))
    target_indexed = target_df.set_index(target_df[join_column].astype(str))

    filtered_pairs: dict[str, str] = {}
    rejected_keys: list[str] = []

    for target_key, source_key in matched_pairs.items():
        if target_key == source_key:
            # Exact key matches were never fuzzy in the first place --
            # nothing to sanity-check, since there's no key-similarity
            # coincidence risk when the keys are literally identical.
            filtered_pairs[target_key] = source_key
            continue

        if len(voting_columns) < 2:
            # Zero columns: no signal at all. Exactly one column: a
            # real value mismatch on that single column is
            # indistinguishable from an unrelated record -- confirmed
            # directly by an existing test's own scenario breaking.
            # Both cases defer rather than guess wrong.
            filtered_pairs[target_key] = source_key
            continue

        source_row = source_indexed.loc[source_key]
        target_row = target_indexed.loc[target_key]

        n_match = sum(
            1 for c in voting_columns
            if source_row[c] == target_row[c]
            or (pd.isna(source_row[c]) and pd.isna(target_row[c]))
        )

        if n_match >= len(voting_columns) / 2:
            filtered_pairs[target_key] = source_key
        else:
            rejected_keys.append(target_key)

    return filtered_pairs, rejected_keys
