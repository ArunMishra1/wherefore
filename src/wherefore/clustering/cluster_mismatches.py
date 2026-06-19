"""
clustering/cluster_mismatches.py

The deterministic layer between the raw DiffResult and the LLM. Two jobs:

  1. GROUP mismatches into clusters worth explaining together. v1
     heuristic: group by column. This is cheap, explainable grouping,
     not ML clustering -- only reach for something fancier (e.g.
     sub-grouping within a column by shape of the diff) if simple
     per-column grouping demonstrably fails on real fixtures.

  2. DETECT: for each cluster, find candidate taxonomy patterns whose
     `detection_hints[0].applies_to_dtypes` matches the cluster's
     column dtype (via taxonomy.registry.patterns_by_dtype), run each
     candidate's signature function (clustering/signatures.py), and
     keep matches at or above `confidence_threshold`. If a pattern
     declares a `confirmation_function`, it's called as a second gate
     before accepting the match (the compound-signature escape hatch
     from taxonomy/schema.py -- not yet exercised by any real pattern,
     since dedup_failure isn't built yet, but the wiring is here).

`confidence_threshold` defaults to 0.9 and is a plain parameter, not a
hardcoded constant -- callers needing stricter matching (e.g. the eval
harness scoring fixtures generated from a controlled synthetic domain,
where a clean signal is expected) can pass 1.0; this is deliberately
NOT domain-aware inside clustering itself. Domain is a synthetic-data
generation concept (see synthetic/base_dataset.py); clustering and
DiffResult have no notion of which domain produced the data they're
given, and should stay that way -- that's what keeps clustering
genuinely reusable on arbitrary real-world CSVs, not just our own
fixtures. Callers who want stricter behavior for a specific context
choose their own threshold; clustering doesn't guess for them.

Output per cluster: candidate_patterns (possibly empty -- "no match
above threshold" is communicated by an empty list, not a special
sentinel) handed to the LLM as either "this looks like X, write the
causal narrative" or, when empty, explicitly unrecognized so the LLM
can say so honestly.

Design reminder: this layer must NOT do the LLM's job for it. It
supplies statistical observations only -- "9 of 9 rows in this cluster
differ by exactly 5 hours" -- never a causal claim like "this is a
timezone bug." See CONTRIBUTING.md: "Why clustering must never make
causal claims."
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wherefore.clustering.signatures import get_signature
from wherefore.comparison.diff_result import DiffResult, MismatchRow
from wherefore.taxonomy.registry import patterns_by_dtype, resolve_import_path

DEFAULT_CONFIDENCE_THRESHOLD = 0.9


@dataclass
class PatternMatch:
    """
    One candidate pattern match for a cluster, with its measured
    confidence. Deliberately just a (pattern_id, confidence, signature)
    tuple of facts -- no narrative, no causal claim. The LLM decides
    whether the statistics actually support attributing the cause to
    this pattern; clustering only reports what it measured.
    """

    pattern_id: str
    signature_name: str
    confidence: float


@dataclass
class Cluster:
    """
    A group of mismatches sharing a column, plus whichever taxonomy
    patterns' signatures fired above threshold for it. `candidate_patterns`
    being empty means no known pattern's statistical signature matched --
    this is the "honestly unrecognized" case, not an error state.
    """

    column: str
    mismatches: list[MismatchRow]
    candidate_patterns: list[PatternMatch] = field(default_factory=list)

    @property
    def is_unrecognized(self) -> bool:
        return len(self.candidate_patterns) == 0


def cluster_mismatches(
    diff_result: DiffResult,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[Cluster]:
    """
    Groups diff_result.mismatches by column, then runs candidate
    pattern detection for each resulting cluster. Returns one Cluster
    per column that has at least one mismatch -- columns with zero
    mismatches don't produce a cluster at all (nothing to explain).
    """
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError(f"confidence_threshold must be in [0, 1], got {confidence_threshold}")

    column_dtypes = {cs.column: cs.target_dtype for cs in diff_result.column_summary}

    clusters: list[Cluster] = []
    for column in diff_result.columns_with_mismatches():
        column_mismatches = diff_result.mismatches_for_column(column)
        dtype = column_dtypes.get(column)

        candidates = _detect_candidates(
            column_mismatches, dtype, confidence_threshold
        )
        clusters.append(
            Cluster(column=column, mismatches=column_mismatches, candidate_patterns=candidates)
        )

    return clusters


def _detect_candidates(
    mismatches: list[MismatchRow],
    dtype: str | None,
    confidence_threshold: float,
) -> list[PatternMatch]:
    if dtype is None:
        return []

    candidates: list[PatternMatch] = []
    for pattern in patterns_by_dtype(dtype):
        # v1: exactly one detection_hint per pattern (see taxonomy/schema.py
        # module docstring on the single-signature decision). If that
        # changes, this will need to check all hints, not just the first.
        hint = pattern.detection_hints[0]
        signature_fn = get_signature(hint.signature)
        confidence = signature_fn(mismatches)

        if confidence < confidence_threshold:
            continue

        if pattern.confirmation_function is not None:
            confirm_fn = resolve_import_path(pattern.confirmation_function)
            if not confirm_fn(mismatches):
                continue

        candidates.append(
            PatternMatch(
                pattern_id=pattern.id,
                signature_name=hint.signature,
                confidence=confidence,
            )
        )

    return candidates
