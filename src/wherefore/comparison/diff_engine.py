"""
comparison/diff_engine.py

NEXT TURN: implement this.

Purpose: thin wrapper around datacompy (per the spec's "wrap an
existing library for the base diff" guidance) that converts its output
into our normalized DiffResult shape. This file should stay genuinely
thin -- if it's accumulating real logic beyond "call datacompy, reshape
the output," that logic probably belongs in key_matching.py or
clustering instead.

Responsibilities:
  - invoke datacompy.Compare with resolved keys from key_matching.py
  - translate datacompy's mismatch/unmatched-rows output into
    list[MismatchRow]
  - preserve dtype info per column (needed by clustering's
    patterns_by_dtype filtering)

Explicitly NOT this file's job: any pattern detection, clustering, or
causal reasoning. This is the line where "detecting THAT things differ"
ends and "explaining WHY" begins -- keeping it a clean boundary is what
lets datacompy be swapped for polars-native diffing later without
touching anything downstream.
"""
