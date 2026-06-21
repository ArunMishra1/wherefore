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
     from taxonomy/schema.py -- not yet exercised by any real pattern;
     neither dedup_failure nor key_mismatch ended up needing it, since
     a single statistical signature was sufficient for both, but the
     wiring is here for the day a pattern genuinely needs it).

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
can say so honestly. candidate_patterns can also legitimately contain
MORE THAN ONE pattern -- see "On multiple legitimate matches" below.

On multiple legitimate matches: clustering deliberately does NOT
suppress or prioritize one candidate over another when more than one
signature fires above threshold for the same cluster, even when one
signature is intuitively "more specific" than another. Real example
discovered while building null_type_coercion: a column where a
genuine null was coerced to the literal string "NULL" produces a
cluster where BOTH null_sentinel_coercion (source=NaT, target="NULL")
AND consistent_value_mapping (the same source value consistently maps
to the same target value -- which is also, technically, true here)
fire at confidence 1.0. It's tempting to add a priority rule
("null_type_coercion should win because its signature is more
specific") -- but doing so would mean clustering is making a CAUSAL
judgment call about which explanation is more plausible, which is
exactly the kind of inference this layer is designed not to make (see
"Design reminder" below). Instead, both candidates are reported
honestly, and disambiguation is left to the reasoning layer, which has
something clustering never will: the actual cited values and the
ability to reason in words about which explanation actually fits (e.g.
"the source being genuinely null, not just present-but-different,
indicates a type-coercion artifact rather than a deliberate category
rename"). See test_cluster_mismatches.py for a test locking in this
behavior as intentional.

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


@dataclass
class RowPresenceMatch:
    """
    Like PatternMatch, but for row-presence patterns (dedup_failure,
    key_mismatch) -- statistical facts only, no causal language, same
    principle as PatternMatch.
    """

    pattern_id: str
    signature_name: str
    confidence: float


@dataclass
class RowPresenceCluster:
    """
    A row-presence finding -- rows present on only one side of the
    comparison, plus whichever row-presence patterns' detectors fired
    for them. Deliberately SEPARATE from Cluster (which groups
    COLUMN-level mismatches): a row that's entirely missing/extra has
    no "source_value -> target_value" pair to report, since there's no
    matched row to compare against. Confirmed by direct testing this
    is a structurally different finding -- a duplicated row (re-
    inserted with a new auto-generated key during a migration retry)
    produces ZERO column-level mismatches; it shows up entirely as an
    extra row in target_only_rows, which the column-based Cluster path
    has no way to see at all.
    """

    side: str  # "source_only" or "target_only"
    rows: list  # list[RowPresenceRecord]
    candidate_patterns: list[RowPresenceMatch] = field(default_factory=list)

    @property
    def is_unrecognized(self) -> bool:
        return len(self.candidate_patterns) == 0


def detect_row_presence_patterns(
    diff_result: DiffResult,
    source_df=None,
    target_df=None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[RowPresenceCluster]:
    """
    Examines diff_result.source_only_rows/target_only_rows for
    row-presence patterns (currently: dedup_failure, key_mismatch) --
    the counterpart to cluster_mismatches() for findings that show up
    as entirely missing/extra rows rather than column-level value
    mismatches. Kept as a SEPARATE function (not folded into
    cluster_mismatches() itself) so the widely-used
    cluster_mismatches() -> list[Cluster] signature and return type
    stay completely unchanged for existing callers; this is purely
    additive.

    `source_df`/`target_df` are optional -- WITHOUT them, dedup_failure
    detection specifically degrades to unrecognized (it needs an
    unmatched row's VALUES checked against the full set of MATCHED
    rows, which isn't available from diff_result alone -- see
    RowPresenceRecord's docstring for why key-only data isn't enough).
    key_mismatch detection does NOT need source_df/target_df -- it only
    compares unmatched rows' KEYS against each other, which
    diff_result already carries -- so it still runs even when neither
    DataFrame is passed. Pass source_df/target_df when available (the
    CLI and eval harness both have them) to get dedup_failure detection
    too.
    """
    if not 0.0 <= confidence_threshold <= 1.0:
        raise ValueError(f"confidence_threshold must be in [0, 1], got {confidence_threshold}")

    clusters: list[RowPresenceCluster] = []

    if diff_result.target_only_rows:
        candidates = _detect_row_presence_candidates(
            unmatched_rows=diff_result.target_only_rows,
            comparison_df=source_df,
            comparison_unmatched_rows=diff_result.source_only_rows,
            join_columns=diff_result.join_columns,
            confidence_threshold=confidence_threshold,
        )
        clusters.append(
            RowPresenceCluster(side="target_only", rows=diff_result.target_only_rows, candidate_patterns=candidates)
        )

    if diff_result.source_only_rows:
        candidates = _detect_row_presence_candidates(
            unmatched_rows=diff_result.source_only_rows,
            comparison_df=target_df,
            comparison_unmatched_rows=diff_result.target_only_rows,
            join_columns=diff_result.join_columns,
            confidence_threshold=confidence_threshold,
        )
        clusters.append(
            RowPresenceCluster(side="source_only", rows=diff_result.source_only_rows, candidate_patterns=candidates)
        )

    return clusters


def _detect_row_presence_candidates(
    unmatched_rows: list,
    comparison_df,
    comparison_unmatched_rows: list,
    join_columns: list[str],
    confidence_threshold: float,
) -> list[RowPresenceMatch]:
    """
    Implements dedup_failure and key_mismatch detection directly (not
    via the taxonomy registry's dtype-based dispatch, since
    row-presence patterns don't have a column/dtype to dispatch on the
    way column-based patterns do -- this is a deliberate, narrower path
    for the two row-presence patterns that exist so far). Both checks
    run independently and BOTH can appear in the result -- a cluster
    being explained by one doesn't preclude the other, same principle
    as cluster_mismatches()'s "on multiple legitimate matches" design
    (see module docstring).

    Confirmed by direct testing this isn't just a theoretical overlap:
    a row whose KEY was merely reformatted (key_mismatch's scenario)
    has, by construction, non-key VALUES that are byte-identical to
    its own original row -- which is mechanically indistinguishable
    from duplicate_content_fraction's "this unmatched row's content
    matches some row elsewhere" check. Both signatures correctly fire
    at confidence 1.0 on the same key_mismatch fixture. Disambiguating
    "reformatted" from "duplicated under a coincidentally similar new
    key" requires comparing the KEYS themselves (close match ->
    reformatting; unrelated -> a genuine duplicate's auto-generated
    key) -- exactly the kind of judgment this layer is designed not to
    make on its own (see "Design reminder" below); reporting both
    candidates honestly and leaving disambiguation to the reasoning
    layer is the correct outcome, not a bug to suppress.

    If/when a third row-presence pattern is added, this should be
    revisited to decide whether a registry-style dispatch is worth the
    complexity at that point, rather than guessing now.
    """
    from wherefore.clustering.signatures import duplicate_content_fraction, key_format_similarity

    candidates: list[RowPresenceMatch] = []

    if comparison_df is not None:
        confidence = duplicate_content_fraction(unmatched_rows, comparison_df, join_columns)
        if confidence >= confidence_threshold:
            candidates.append(
                RowPresenceMatch(
                    pattern_id="dedup_failure",
                    signature_name="duplicate_content_fraction",
                    confidence=confidence,
                )
            )

    confidence = key_format_similarity(unmatched_rows, comparison_unmatched_rows, join_columns)
    if confidence >= confidence_threshold:
        candidates.append(
            RowPresenceMatch(
                pattern_id="key_mismatch",
                signature_name="key_format_similarity",
                confidence=confidence,
            )
        )

    return candidates


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
