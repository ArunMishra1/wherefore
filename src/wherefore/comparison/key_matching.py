"""
comparison/key_matching.py

NEXT TURN: implement this.

Purpose: resolve join keys between source and target when they don't
align exactly. Two distinct sub-problems:

  1. Exact match: same key column(s), same values -- trivial case,
     just validate uniqueness and hand off to diff_engine.
  2. Fuzzy match: key columns renamed, reformatted (e.g. "CUST-001" vs
     "CUST001"), or composite keys that need normalization before
     matching. Use rapidfuzz for string similarity; need a clear
     confidence threshold below which we refuse to auto-match and
     instead surface "could not confidently match these N rows" rather
     than silently guessing wrong and attributing a fuzzy-match error
     to some other taxonomy pattern.

Important: fuzzy key matching mistakes are themselves a taxonomy
pattern (key_mismatch / fuzzy join issues) -- so this module's
confidence scores need to be exposed in DiffResult, not just used
internally and discarded, since low-confidence matches are exactly the
signal that pattern's detector needs.
"""
