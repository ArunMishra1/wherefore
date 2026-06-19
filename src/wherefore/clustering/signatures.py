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


SIGNATURE_REGISTRY: dict[str, Callable[[list[MismatchRow]], float]] = {
    "constant_offset_subset": constant_offset_subset,
}


def get_signature(name: str) -> Callable[[list[MismatchRow]], float]:
    if name not in SIGNATURE_REGISTRY:
        raise KeyError(
            f"Unknown signature: {name!r}. "
            f"Registered signatures: {sorted(SIGNATURE_REGISTRY.keys())}"
        )
    return SIGNATURE_REGISTRY[name]
