"""
clustering/signatures.py

The actual detector functions that taxonomy YAML files reference by
string key (e.g. "constant_offset_subset" in timezone_shift.yaml).
Kept separate from cluster_mismatches.py so that:
  - adding a new signature is additive (register a function here,
    reference it by name in a new pattern's YAML)
  - signature functions are independently unit-testable against real
    MismatchRow data without needing the full clustering pipeline

Every signature function takes `list[MismatchRow]` (already filtered
to one column by the caller -- see cluster_mismatches.py) and returns
a confidence float in [0, 1]. PURELY STATISTICAL -- no causal language,
no pattern naming beyond what's mechanically measured. See
CONTRIBUTING.md: "Why clustering must never make causal claims."
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

import numpy as np
import pandas as pd

from wherefore.comparison.diff_result import MismatchRow


def constant_offset_subset(mismatches: list[MismatchRow]) -> float:
    """
    Confidence that mismatched values in this cluster differ from
    their source by the same constant delta -- the signature
    timezone_shift.yaml's detection_hints describes.

    Computes target - source for every mismatch, finds the most
    common delta, and returns the fraction of mismatches sharing that
    exact delta. Deliberately tolerant of a minority of differently-
    shifted or unrelated outliers within the same cluster (e.g. a
    cluster might catch both a timezone bug AND a handful of unrelated
    data-entry errors in the same column) -- requiring a literal 100%
    match would make this signature brittle on real-world data, where
    failure causes are rarely perfectly clean.

    Returns 0.0 (not an error) for non-subtractable values or an empty
    cluster -- absence of the signature is a valid, informative result,
    not a failure to compute one.
    """
    if not mismatches:
        return 0.0

    deltas = []
    for m in mismatches:
        try:
            deltas.append(m.target_value - m.source_value)
        except TypeError:
            # Values that can't be subtracted (e.g. one side is null,
            # or the two sides are genuinely incomparable types) don't
            # count as evidence FOR this signature -- they're simply
            # excluded, not penalized.
            continue

    if not deltas:
        return 0.0

    delta_counts = Counter(deltas)
    most_common_delta, most_common_count = delta_counts.most_common(1)[0]

    # A delta of exactly zero isn't a "shift" -- it would mean the
    # values being compared are actually equal, which shouldn't appear
    # in a mismatches list in the first place, but guard against it
    # explicitly rather than reporting false confidence on a no-op.
    if most_common_delta == type(most_common_delta)(0):
        return 0.0

    return most_common_count / len(deltas)


def truncated_prefix(mismatches: list[MismatchRow]) -> float:
    """
    Confidence that mismatched values in this cluster are explained by
    truncation: the target value is a literal prefix of the source
    value, and the target is shorter. Computed as the fraction of
    mismatches satisfying this prefix relationship.

    Deliberately does NOT require every mismatch to be cut to the
    SAME length -- different rows can be truncated to different
    lengths in practice (e.g. a fixed byte-length limit on a
    multi-byte-encoded string truncates different strings to different
    character counts). What's diagnostic is the prefix relationship
    itself, not a shared cut length -- unlike constant_offset_subset,
    where the constant delta IS the signature.

    Returns 0.0 for an empty cluster or when no mismatches show this
    relationship (e.g. a genuinely different value, not a cut-down one).
    """
    if not mismatches:
        return 0.0

    prefix_count = 0
    total_comparable = 0
    for m in mismatches:
        source_str = m.source_value
        target_str = m.target_value
        if source_str is None or target_str is None:
            continue
        source_str, target_str = str(source_str), str(target_str)
        total_comparable += 1
        if len(target_str) < len(source_str) and source_str.startswith(target_str):
            prefix_count += 1

    if total_comparable == 0:
        return 0.0

    return prefix_count / total_comparable


def consistent_value_mapping(mismatches: list[MismatchRow]) -> float:
    """
    Confidence that mismatches in this cluster are explained by a
    systematic value recode: for each distinct source value seen in
    the cluster, does it ALWAYS map to the same target value? This is
    the signature of enum_drift.yaml's detection_hints -- a real
    migration recode ('approved' -> 'APPROVED') is a consistent
    function from source value to target value, not noise.

    Computed per distinct source value, then averaged (weighted by
    how many mismatches each source value contributes) -- so a cluster
    where 'approved' consistently maps to 'APPROVED' but 'denied'
    inconsistently maps to different things on different rows scores
    partial confidence proportional to how much of the cluster is
    explained by a clean mapping, not an all-or-nothing pass/fail
    across the whole cluster.

    REQUIRES REPETITION TO COUNT AS EVIDENCE: a source value that
    appears only ONCE in the cluster contributes nothing to the
    confidence score, regardless of what it mapped to. Confirmed by
    direct testing that without this guard, a column of unique
    free-text values (e.g. truncated names, where every person's name
    is different) trivially scores 1.0 here too -- every "source value"
    appears once, so it's vacuously "consistent" with itself, which is
    a real false-positive risk once enum_drift and truncation compete
    for the same string-dtype clusters. A genuine recode is only
    visible as a pattern across REPEATED source values; a column where
    nothing repeats can't demonstrate that pattern at all and should
    score 0 contribution from those rows, not 1.

    Returns 0.0 for an empty cluster, or when no source value repeats
    (nothing to measure consistency across).
    """
    if not mismatches:
        return 0.0

    by_source_value: dict[object, list[object]] = {}
    for m in mismatches:
        by_source_value.setdefault(m.source_value, []).append(m.target_value)

    total = len(mismatches)
    consistent_count = 0
    for source_value, target_values in by_source_value.items():
        if len(target_values) < 2:
            continue  # a single occurrence proves nothing about consistency
        most_common_target_count = Counter(target_values).most_common(1)[0][1]
        consistent_count += most_common_target_count

    if consistent_count == 0:
        return 0.0

    return consistent_count / total


_NULL_SENTINEL_STRINGS = {"null", "n/a", "none", "na", "nil", "undefined", "missing"}


def null_sentinel_coercion(mismatches: list[MismatchRow]) -> float:
    """
    Confidence that mismatches in this cluster are explained by a
    genuine null being written out as a literal sentinel string (e.g.
    NaN/NaT/None on the source becoming the text "NULL" on the
    target) -- the signature null_type_coercion.yaml's detection_hints
    describes.

    A mismatch counts as evidence FOR this signature only if exactly
    one side is genuinely null (via pd.isna()) and the OTHER side is a
    non-null value whose lowercased string form is a known null-like
    sentinel (see _NULL_SENTINEL_STRINGS). This direction-agnostic
    design (source-null-target-sentinel OR target-null-source-sentinel)
    covers migrations going either way.

    Deliberately does NOT count "both sides null but represented
    differently" (e.g. NaN vs NaT) as evidence -- that's not a type
    coercion bug, both sides are still genuinely null, just via
    different pandas null representations; nothing to report there.

    Returns 0.0 for an empty cluster or when no mismatches show this
    relationship -- e.g. a column where the mismatch is two different
    REAL values (not a null-vs-sentinel case) correctly scores 0.0, not
    a false positive.
    """
    if not mismatches:
        return 0.0

    evidence_count = 0
    for m in mismatches:
        source_is_null = _is_null(m.source_value)
        target_is_null = _is_null(m.target_value)

        if source_is_null and target_is_null:
            continue  # both genuinely null, just different representations -- not evidence
        if source_is_null and _is_null_sentinel_string(m.target_value):
            evidence_count += 1
        elif target_is_null and _is_null_sentinel_string(m.source_value):
            evidence_count += 1

    return evidence_count / len(mismatches)


def _is_null(value) -> bool:
    try:
        result = pd.isna(value)
        # pd.isna on an array-like returns an array; we only ever pass
        # scalars here, but guard explicitly rather than let a stray
        # array silently break the truthiness check below.
        return bool(result)
    except (TypeError, ValueError):
        return False


def _is_null_sentinel_string(value) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() in _NULL_SENTINEL_STRINGS


def float32_precision_drift(mismatches: list[MismatchRow]) -> float:
    """
    Confidence that mismatches in this cluster are explained by values
    being rounded through float32 precision -- the signature
    float_precision.yaml's detection_hints describes. A real migration
    cause: a column defined as REAL/FLOAT4 on the target instead of
    DOUBLE/FLOAT8, or a value passing through a lower-precision
    serialization step.

    Checks the EXACT float32 round-trip: does float(np.float32(source))
    equal the target value? This is a deterministic, verifiable fact
    about the data, not a magnitude-based heuristic.

    A magnitude-based heuristic (e.g. "relative delta below some small
    threshold") was tried first and rejected after direct testing
    surfaced a real false-positive: a one-cent rounding bug on a large
    value (98762.17 -> 98762.18) has a relative magnitude small enough
    to fall within a reasonable float32-precision threshold, even
    though it has NOTHING to do with float32 rounding -- 98762.17
    actually rounds to 98762.171875 in float32, not 98762.18. Pure
    magnitude can't distinguish "coincidentally small relative change"
    from "this specific value's actual float32 representation," but
    the exact round-trip check can, since it's checking the real
    mechanism rather than approximating its size.

    Returns 0.0 for an empty cluster or non-numeric values.
    """
    if not mismatches:
        return 0.0

    evidence_count = 0
    total_comparable = 0
    for m in mismatches:
        try:
            source_val = float(m.source_value)
            target_val = float(m.target_value)
        except (TypeError, ValueError):
            continue

        total_comparable += 1
        if source_val == target_val:
            continue  # not a mismatch at all; shouldn't normally appear here, but not evidence either way
        if float(np.float32(source_val)) == target_val:
            evidence_count += 1

    if total_comparable == 0:
        return 0.0

    return evidence_count / total_comparable


def mojibake_reversible(mismatches: list[MismatchRow]) -> float:
    """
    Confidence that mismatches in this cluster are explained by a
    UTF-8 vs Latin-1 encoding mismatch. Checks the EXACT mechanism
    directly: does target_value.encode('latin-1').decode('utf-8')
    recover source_value exactly? Real mojibake is deterministically
    reversible this way -- confirmed by direct testing this is the
    literal inverse of how the corruption is produced, not an
    approximation via regex over "suspicious" characters.

    Returns 0.0 for an empty cluster or non-string values.
    """
    if not mismatches:
        return 0.0

    evidence_count = 0
    total_comparable = 0
    for m in mismatches:
        if not isinstance(m.source_value, str) or not isinstance(m.target_value, str):
            continue
        total_comparable += 1
        try:
            recovered = m.target_value.encode("latin-1").decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        if recovered == m.source_value:
            evidence_count += 1

    if total_comparable == 0:
        return 0.0

    return evidence_count / total_comparable


def key_format_similarity(
    unmatched_rows: list,
    comparison_unmatched_rows: list,
    join_columns: list[str],
) -> float:
    """
    Confidence that unmatched rows (rows present on only one side of
    the comparison) are explained by key_mismatch: for each unmatched
    row, does its key NORMALIZE (separators stripped, case folded) to
    the same string as some unmatched row's key on the OTHER side? A
    record whose key was merely reformatted normalizes identically to
    its counterpart; a genuinely new/removed record's key won't,
    UNLESS it happens to collide after normalization too -- which is
    exactly as informative a "no" as an exact-key collision would be.

    A raw rapidfuzz fuzz.ratio threshold was tried first and rejected
    after direct testing surfaced a real false positive: two
    GENUINELY UNRELATED records in the same domain (e.g.
    "ACCT-100002" and "ACCT-100022" -- different accounts, not a
    reformat of one another) score ~91 against each other purely from
    sharing a long common prefix and digit-heavy format -- well above
    the 75 floor key_matching.py uses, and not reliably separable from
    a genuine reformat's ~93-95 score by tuning the threshold alone
    (the gap between "false positive" and "true positive" scores
    wasn't clean enough to draw a line through; see key_matching.py's
    own docstring on this same shared-characters risk). A normalization
    EQUALITY check has no such gradient to mistune: either two keys are
    the same record's key under a deterministic transform, or they
    aren't. This mirrors float32_precision_drift and mojibake_reversible
    elsewhere in this module, both of which check the literal reverse
    mechanism rather than approximating it with a magnitude/score
    heuristic, for the same reason.

    Only join_columns[0] is used for composite keys -- v1 simplicity
    matching dedup_failure's own scope; multi-column key normalization
    would need to decide how to combine per-column results, which isn't
    needed by any real fixture yet.

    Returns 0.0 if either side has no unmatched rows to compare,
    consistent with duplicate_content_fraction's empty-input contract.
    """
    if not unmatched_rows or not comparison_unmatched_rows:
        return 0.0

    key_col = join_columns[0]
    comparison_normalized = {
        _normalize_key(str(r.key.get(key_col))) for r in comparison_unmatched_rows
    }

    match_count = sum(
        1
        for record in unmatched_rows
        if _normalize_key(str(record.key.get(key_col))) in comparison_normalized
    )

    return match_count / len(unmatched_rows)


def _normalize_key(key: str) -> str:
    """The deterministic transform key_format_similarity checks for
    equality under: separators stripped, case folded. Mirrors the
    exact reformat synthetic/corruptors/key_mismatch.py's corruptor
    applies, so a real injected key_mismatch fixture normalizes to an
    exact match by construction -- not an approximation of it."""
    return key.replace("-", "").replace("_", "").lower()


def duplicate_content_fraction(unmatched_rows: list, comparison_df, join_columns: list[str]) -> float:
    """
    Confidence that unmatched rows (rows present on only one side of
    the comparison) are explained by dedup_failure: for each unmatched
    row, does its non-key VALUE CONTENT exactly match some row in
    `comparison_df` (the OTHER side's full dataset, including matched
    rows)? A row that's a genuine new/removed record won't match
    anything; a row that's a duplicate re-inserted under a new
    auto-generated key will match an existing row's content exactly.

    Deliberately NOT in SIGNATURE_REGISTRY alongside the other
    signature functions -- this function's shape genuinely differs
    (it needs the full comparison DataFrame and join_columns, not just
    a list[MismatchRow]), since row-presence patterns don't have a
    column/dtype to dispatch on the way column-based mismatch patterns
    do. Called directly from cluster_mismatches.py's
    _detect_row_presence_candidates.

    Returns 0.0 if unmatched_rows is empty or comparison_df is None.
    """
    if not unmatched_rows or comparison_df is None:
        return 0.0

    non_key_cols = [c for c in comparison_df.columns if c not in join_columns]
    if not non_key_cols:
        return 0.0

    comparison_value_tuples = set(
        tuple(row) for row in comparison_df[non_key_cols].itertuples(index=False)
    )

    duplicate_count = 0
    for record in unmatched_rows:
        try:
            value_tuple = tuple(record.values.get(c) for c in non_key_cols)
        except AttributeError:
            continue
        if value_tuple in comparison_value_tuples:
            duplicate_count += 1

    return duplicate_count / len(unmatched_rows)


SIGNATURE_REGISTRY: dict[str, Callable[[list[MismatchRow]], float]] = {
    "constant_offset_subset": constant_offset_subset,
    "truncated_prefix": truncated_prefix,
    "consistent_value_mapping": consistent_value_mapping,
    "null_sentinel_coercion": null_sentinel_coercion,
    "float32_precision_drift": float32_precision_drift,
    "mojibake_reversible": mojibake_reversible,
}


def get_signature(name: str) -> Callable[[list[MismatchRow]], float]:
    if name not in SIGNATURE_REGISTRY:
        raise KeyError(
            f"Unknown signature: {name!r}. "
            f"Registered signatures: {sorted(SIGNATURE_REGISTRY.keys())}"
        )
    return SIGNATURE_REGISTRY[name]
