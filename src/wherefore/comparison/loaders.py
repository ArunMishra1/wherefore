"""
comparison/loaders.py

NEXT TURN: implement this.

Purpose: load CSV/JSON into normalized pandas DataFrames, handling:
  - encoding detection/declaration (this matters doubly here since
    encoding_mismatch is itself a taxonomy pattern -- the loader needs
    to NOT silently fix encoding issues that the user actually wants
    detected and explained)
  - schema inference vs. user-supplied schema
  - consistent null representation across CSV ("", "NULL", "NaN") and
    JSON (null) before comparison, so we're not flagging
    loader-introduced inconsistency as a "real" mismatch

Key design tension to resolve here: loaders need to be "dumb" enough
that genuine source-data problems (encoding mismatches, type coercion
issues) survive into the DiffResult for the taxonomy to detect, while
being "smart" enough not to crash on minor formatting variance that
isn't the point of the tool. Document every normalization decision
made here explicitly, since each one is a judgment call about what
counts as signal vs. noise.
"""
