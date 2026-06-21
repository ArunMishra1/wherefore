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
