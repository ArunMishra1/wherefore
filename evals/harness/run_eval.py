"""
evals/harness/run_eval.py

Orchestrates a full eval run against every fixture in
evals/fixtures/: loads each fixture's ground truth, runs the REAL
pipeline (load CSV -> diff -> cluster, and optionally -> explain), and
scores the result against ground truth via scoring.py.

Two modes:
  - Statistical (always runs, free, no API key needed): scores
    whether cluster_mismatches()'s candidate_patterns correctly
    identifies the injected pattern via its statistical signature
    alone.
  - LLM (opt-in via --llm flag, costs real money, requires
    ANTHROPIC_API_KEY): additionally scores whether explain()'s
    matched_pattern_id agrees with ground truth. Off by default for
    the same reason --explain is off by default in the CLI.

Note on what's actually being scored for each fixture: ground truth
records which COLUMN was corrupted (affected_column) and what pattern
was injected there. Scoring looks specifically at the cluster for
affected_column -- not just "did ANY cluster in this fixture match,"
which would be a weaker and less precise claim.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from wherefore.clustering.cluster_mismatches import cluster_mismatches
from wherefore.comparison.diff_engine import compare
from wherefore.comparison.loaders import load_csv
from wherefore.reasoning.explain import explain
from wherefore.synthetic.ground_truth import GroundTruth, list_fixture_ids, load_fixture
from wherefore.taxonomy.registry import build_llm_taxonomy_menu

from evals.harness.scoring import ScoredCase, score_pattern_match, score_pattern_match_against_candidates, summarize_run

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _ground_truth_pattern_id(gt: GroundTruth) -> str | None:
    """
    The pattern_id ACTUALLY injected, or None if no real corruption
    was injected (the "genuinely unrecognized" fixture type). All
    fixtures built so far inject at most one corruption, so this takes
    the first (and only) entry; a future multi-corruption fixture
    would need scoring logic that handles multiple affected columns,
    which isn't built yet -- flagging rather than silently assuming.
    """
    if not gt.injected_corruptions:
        return None
    if len(gt.injected_corruptions) > 1:
        raise NotImplementedError(
            f"Fixture {gt.fixture_id} has multiple injected_corruptions; "
            "scoring logic for multi-corruption fixtures isn't built yet."
        )
    return gt.injected_corruptions[0].pattern_id


def _affected_column(gt: GroundTruth) -> str | None:
    if not gt.injected_corruptions:
        return None
    return gt.injected_corruptions[0].affected_column


def _target_clusters_for_fixture(gt, fixtures_dir):
    affected_column = _affected_column(gt)
    source_df = load_csv(fixtures_dir / gt.source_file)
    target_df = load_csv(fixtures_dir / gt.target_file)
    diff_result = compare(source_df, target_df, join_columns=gt.join_column)
    clusters = cluster_mismatches(diff_result)

    if affected_column is not None:
        return [c for c in clusters if c.column == affected_column], affected_column
    return clusters, affected_column


def run_statistical_eval(fixtures_dir: Path = FIXTURES_DIR) -> list[ScoredCase]:
    """
    Runs the real load -> diff -> cluster pipeline against every
    fixture and scores clustering's statistical match: did the true
    pattern appear ANYWHERE among the cluster's candidate_patterns,
    not just as the first one. See scoring.py's
    score_pattern_match_against_candidates for why this set-membership
    test, not exact-equality, is the correct one for clustering's
    output specifically.
    """
    scored_cases = []
    for fixture_id in list_fixture_ids(fixtures_dir):
        gt = load_fixture(fixture_id, fixtures_dir)
        actual_pattern_id = _ground_truth_pattern_id(gt)
        target_clusters, affected_column = _target_clusters_for_fixture(gt, fixtures_dir)

        if not target_clusters:
            predicted_pattern_ids = []
        else:
            cluster = target_clusters[0]
            predicted_pattern_ids = [p.pattern_id for p in cluster.candidate_patterns]

        outcome = score_pattern_match_against_candidates(actual_pattern_id, predicted_pattern_ids)
        # For ScoredCase's single predicted_pattern_id field, report the
        # true pattern if it was among the candidates (so a correct
        # multi-candidate match still displays as itself, not
        # misleadingly as whichever candidate happened to be first),
        # otherwise the first predicted candidate (or None) for visibility.
        if actual_pattern_id in predicted_pattern_ids:
            predicted_pattern_id = actual_pattern_id
        else:
            predicted_pattern_id = predicted_pattern_ids[0] if predicted_pattern_ids else None

        scored_cases.append(
            ScoredCase(
                fixture_id=fixture_id,
                column=affected_column or "(none)",
                actual_pattern_id=actual_pattern_id,
                predicted_pattern_id=predicted_pattern_id,
                outcome=outcome,
            )
        )

    return scored_cases


def run_llm_eval(fixtures_dir: Path = FIXTURES_DIR) -> list[ScoredCase]:
    """
    Like run_statistical_eval, but additionally calls the real
    explain() for the target cluster and scores its matched_pattern_id
    instead of clustering's raw statistical match. Requires
    ANTHROPIC_API_KEY -- explain() will raise RuntimeError if it's not set.
    """
    taxonomy_menu = build_llm_taxonomy_menu()
    scored_cases = []

    for fixture_id in list_fixture_ids(fixtures_dir):
        gt = load_fixture(fixture_id, fixtures_dir)
        actual_pattern_id = _ground_truth_pattern_id(gt)
        target_clusters, affected_column = _target_clusters_for_fixture(gt, fixtures_dir)

        if not target_clusters:
            predicted_pattern_id = None
        else:
            explanation, _redaction_categories = explain(target_clusters[0], taxonomy_menu)
            predicted_pattern_id = explanation.matched_pattern_id

        outcome = score_pattern_match(actual_pattern_id, predicted_pattern_id)
        scored_cases.append(
            ScoredCase(
                fixture_id=fixture_id,
                column=affected_column or "(none)",
                actual_pattern_id=actual_pattern_id,
                predicted_pattern_id=predicted_pattern_id,
                outcome=outcome,
            )
        )

    return scored_cases


def print_report(label: str, scored_cases: list[ScoredCase]) -> None:
    summary = summarize_run(scored_cases)
    print(f"=== {label} ===")
    print(f"Total cases: {summary.total_cases}")
    print(f"Overall accuracy (correct match + honest abstain): {summary.overall_accuracy():.2%}")
    print(f"Outcome breakdown: {summary.outcome_counts}")
    print()
    for pattern_id, m in sorted(summary.metrics_by_pattern.items()):
        precision = f"{m.precision:.2f}" if m.precision is not None else "N/A"
        recall = f"{m.recall:.2f}" if m.recall is not None else "N/A"
        print(
            f"  {pattern_id}: precision={precision} recall={recall} "
            f"(TP={m.true_positives} FP={m.false_positives} FN={m.false_negatives})"
        )
    print()
    for case in scored_cases:
        print(f"  [{case.outcome.value}] {case.fixture_id}: actual={case.actual_pattern_id}, predicted={case.predicted_pattern_id}")
    print()


def main() -> None:
    include_llm = "--llm" in sys.argv

    if include_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Error: --llm requires ANTHROPIC_API_KEY to be set in your environment.\n"
            'Run: export ANTHROPIC_API_KEY="sk-ant-..." before using --llm.',
            file=sys.stderr,
        )
        sys.exit(1)

    statistical_cases = run_statistical_eval()
    print_report("Statistical eval (clustering only, free, no API key)", statistical_cases)

    if include_llm:
        llm_cases = run_llm_eval()
        print_report("LLM eval (explain(), real API calls)", llm_cases)
    else:
        print("Skipping LLM eval (pass --llm to run it; requires ANTHROPIC_API_KEY, makes real API calls).")


if __name__ == "__main__":
    main()
