"""
evals/harness/scoring.py

NEXT TURN: implement this.

Purpose: pure scoring functions, given (predictions, ground_truth) ->
metrics. Kept separate from run_eval.py's orchestration so scoring
logic is independently unit-testable with hand-constructed fixtures
(no need to run the real pipeline or call an LLM to test that
precision/recall math is correct).

Planned metrics, per corruption type (taxonomy pattern_id):
  - precision: of clusters the system matched to pattern X, what
    fraction were ACTUALLY pattern X per ground truth?
  - recall: of fixtures where pattern X was actually injected, what
    fraction did the system correctly identify?
  - confusion matrix across all pattern_ids + "unrecognized" as a
    pseudo-class -- this is important: a system that says
    "unrecognized" on a genuinely novel/edge-case corruption should
    NOT be scored as a failure the same way as confidently naming the
    WRONG pattern. Track these as distinct outcome types:
        true_positive: correct pattern matched
        false_positive: wrong pattern matched confidently
        honest_abstain: correctly said "unrecognized" for an
            out-of-taxonomy or ambiguous case
        false_abstain: said "unrecognized" but a real pattern was
            injected and should have been detectable
        false_negative: wrong pattern OR missed entirely

Also worth scoring separately (per ground_truth.py's affected_rows
design): row-level cluster accuracy -- did the system group the RIGHT
rows together, independent of whether it named the cluster correctly?
This catches a failure mode pattern-only scoring would miss: correct
pattern name, wrong rows attributed to it.
"""
