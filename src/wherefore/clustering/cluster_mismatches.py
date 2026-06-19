"""
clustering/cluster_mismatches.py

NEXT TURN: implement this.

Purpose: the deterministic layer between the raw DiffResult and the
LLM. Two jobs:

  1. GROUP mismatches into clusters worth explaining together --
     initial heuristic: group by column, then sub-group by rough shape
     of the diff (e.g. "all values increased," "all values are
     null-where-source-had-data"). This is cheap, explainable grouping,
     not ML clustering -- start simple, only reach for something
     fancier (e.g. embedding similarity on diff descriptions) if simple
     grouping demonstrably fails on real fixtures.

  2. DETECT: for each cluster, check every taxonomy pattern whose
     `detection_hints[0].applies_to_dtypes` matches the cluster's
     column dtype (via taxonomy.registry.patterns_by_dtype), run the
     signature check (see signatures.py, to be created), and if a
     pattern's `confirmation_function` is set, call it as a second
     gate before accepting the match.

  Output per cluster: either (a) one or more candidate pattern_ids with
  confidence, handed to the LLM as "this looks like X, write the causal
  narrative," or (b) no candidates, handed to the LLM explicitly labeled
  unrecognized so it can say so honestly rather than confabulating a
  pattern that doesn't apply.

Design reminder (from project context): this layer must NOT do the
LLM's job for it. It supplies statistical observations only --
"these 12 rows differ by exactly 5 hours" -- never a causal claim like
"this is a timezone bug." Causal attribution and narrative are the
LLM's job; if this file starts asserting causes, the AI layer becomes
decorative and the eval becomes meaningless (it would just be testing
whether the LLM repeats what clustering already concluded).
"""
