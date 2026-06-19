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
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

DEFAULT_MIN_CONFIDENCE = 75.0
DEFAULT_AMBIGUITY_GAP = 5.0


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
