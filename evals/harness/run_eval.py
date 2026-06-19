"""
evals/harness/run_eval.py

NEXT TURN: implement this.

Purpose: orchestrates a full eval run --
  1. for each fixture pair in evals/fixtures/, load ground_truth.json
  2. run the real pipeline (loaders -> key_matching -> diff_engine ->
     cluster_mismatches -> reasoning.explain) end to end, exactly as
     the CLI would, so the eval tests the actual user-facing behavior
     and not a shortcut path
  3. for each cluster's ClusterExplanation, compare matched_pattern_id
     (and ideally affected rows) against the corresponding
     injected_corruptions entry in ground_truth.json
  4. hand results to scoring.py, write a timestamped results file to
     evals/results/

Should record, per run: prompt version used (from
reasoning/prompts/*.md filename), model/provider used, git commit
hash, and timestamp -- this is what makes "X% accuracy" claims in the
README reproducible and attributable to a specific version of the
system rather than a vague historical claim.

Should be runnable as both a pytest-collected test (so `pytest evals/`
gives a pass/fail signal in CI, e.g. "precision must stay above 0.8")
AND as a standalone script for generating the full results report
(`python -m evals.harness.run_eval --report`).
"""
