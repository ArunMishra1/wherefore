"""
reasoning/explain.py

NEXT TURN: implement this.

Purpose: the single `explain()` interface the rest of the codebase
calls, regardless of which underlying model/provider answers it. This
is what makes the model swappable later, per the spec.

Planned shape:

    def explain(cluster: MismatchCluster, candidate_patterns: list[PatternMatch]) -> ClusterExplanation:
        provider = get_provider()  # reads config/env, returns a Provider instance
        prompt = build_prompt(cluster, candidate_patterns)  # from prompts/ templates
        raw_response = provider.complete(prompt)
        return parse_explanation(raw_response)  # validated against ClusterExplanation schema

`Provider` is an ABC (providers/base.py) with one required method,
`complete(prompt: str) -> str`. providers/claude.py implements it
against the Anthropic SDK. Swapping models later means writing a new
Provider subclass -- nothing else in this file or upstream changes.

ClusterExplanation (return type, to be defined likely in this file or
a sibling schema.py) should capture, at minimum:
  - matched_pattern_id: str | None  (None = "unrecognized pattern")
  - confidence: float
  - narrative: str  (the plain-English explanation)
  - cited_example_rows: list[...]  (specific rows referenced, per spec
    requirement that the report cites real examples per cluster)

This return shape IS the eval target -- evals/harness/scoring.py
compares `matched_pattern_id` against the synthetic ground truth label
to compute precision/recall per corruption type. Keep this schema
stable once evals are running against it.
"""
