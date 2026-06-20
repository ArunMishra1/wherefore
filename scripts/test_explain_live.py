"""
scripts/test_explain_live.py

Run this LOCALLY, on your own machine, with your own ANTHROPIC_API_KEY
set in your shell -- never paste a real key into a chat message.

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    python3 scripts/test_explain_live.py

What this does: generates one real corrupted fixture for each of the
three working taxonomy patterns (timezone_shift, truncation,
enum_drift), runs each through the actual pipeline (diff -> cluster),
then calls explain() against the REAL Claude API for each cluster and
prints the resulting narrative. Also runs one deliberately
"unrecognized" case (random, non-matching corruption) to see how the
model handles a cluster with no statistical pattern match.

Nothing here writes any file or makes any network call except the
explicit calls to the Anthropic API via explain(). No key is read or
written anywhere except from the ANTHROPIC_API_KEY environment
variable already present in your shell.
"""

from __future__ import annotations

import os
import sys

# Make sure we're running from inside an environment where `wherefore`
# is installed (e.g. after `pip install -e ".[dev]"` per the README).
try:
    from wherefore.synthetic.base_dataset import (
        FINANCIAL_ACCOUNTS,
        HEALTHCARE_PATIENTS,
        generate_dataset,
    )
    from wherefore.synthetic.corruptors.timezone_shift import apply as shift_timezone
    from wherefore.synthetic.corruptors.truncation import apply as truncate
    from wherefore.synthetic.corruptors.enum_drift import apply as drift_enum
    from wherefore.comparison.diff_engine import compare
    from wherefore.clustering.cluster_mismatches import cluster_mismatches
    from wherefore.reasoning.explain import explain
    from wherefore.taxonomy.registry import build_llm_taxonomy_menu
except ImportError as e:
    print(f"Import failed: {e}")
    print("Make sure you've run `pip install -e \".[dev]\"` inside the activated venv first.")
    sys.exit(1)


def check_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set in this shell.")
        print('Run: export ANTHROPIC_API_KEY="sk-ant-..." before running this script.')
        sys.exit(1)
    print(f"Using ANTHROPIC_API_KEY (length: {len(os.environ['ANTHROPIC_API_KEY'])} chars) -- not printing the value.\n")


def print_explanation(label: str, explanation) -> None:
    print(f"--- {label} ---")
    print(f"matched_pattern_id : {explanation.matched_pattern_id}")
    print(f"confidence (LLM)   : {explanation.confidence}")
    print(f"narrative          : {explanation.narrative}")
    if explanation.cited_rows:
        print("cited_rows:")
        for row in explanation.cited_rows:
            print(f"  - {row.key}: {row.source_value!r} -> {row.target_value!r}")
    else:
        print("cited_rows: (none)")
    print()


def main() -> None:
    check_api_key()
    menu = build_llm_taxonomy_menu()

    print("=" * 70)
    print("TEST 1: timezone_shift (financial accounts domain)")
    print("=" * 70)
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=42)
    target, _ = shift_timezone(source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1)
    result = compare(source, target, join_columns="account_id")
    clusters = cluster_mismatches(result)
    for c in clusters:
        explanation = explain(c, menu)
        print_explanation(f"column={c.column} (statistical match: {c.candidate_patterns})", explanation)

    print("=" * 70)
    print("TEST 2: truncation (healthcare patients domain)")
    print("=" * 70)
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    target, _ = truncate(source, column="patient_name", max_length=8, affected_fraction=0.5, seed=1)
    result = compare(source, target, join_columns="patient_id")
    clusters = cluster_mismatches(result)
    for c in clusters:
        explanation = explain(c, menu)
        print_explanation(f"column={c.column} (statistical match: {c.candidate_patterns})", explanation)

    print("=" * 70)
    print("TEST 3: enum_drift (healthcare patients domain)")
    print("=" * 70)
    source = generate_dataset(HEALTHCARE_PATIENTS, n_rows=30, seed=42)
    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target, _ = drift_enum(source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=1)
    result = compare(source, target, join_columns="patient_id")
    clusters = cluster_mismatches(result)
    for c in clusters:
        explanation = explain(c, menu)
        print_explanation(f"column={c.column} (statistical match: {c.candidate_patterns})", explanation)

    print("=" * 70)
    print("TEST 4: genuinely unrecognized (random, non-matching corruption)")
    print("=" * 70)
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=42)
    target = source.copy()
    target.loc[0, "account_type"] = "xq7z"
    target.loc[1, "account_type"] = "random_garbage_2"
    target.loc[2, "account_type"] = "totally_different_value"
    result = compare(source, target, join_columns="account_id")
    clusters = cluster_mismatches(result)
    for c in clusters:
        explanation = explain(c, menu)
        print_explanation(f"column={c.column} (statistical match: {c.candidate_patterns}, unrecognized={c.is_unrecognized})", explanation)

    print("Done. Paste this output back to Claude for review.")


if __name__ == "__main__":
    main()
