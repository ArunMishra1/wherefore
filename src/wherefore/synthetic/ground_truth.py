"""
synthetic/ground_truth.py

NEXT TURN: implement this.

Purpose: writes ground_truth.json alongside each generated
source/target fixture pair. This file is THE eval answer key -- it's
what scoring.py compares the LLM's matched_pattern_id against.

Planned shape, one entry per corruption applied (a single fixture pair
may have multiple corruptions injected, e.g. timezone_shift AND
enum_drift both applied to create a more realistic multi-cause
scenario):

    {
      "fixture_id": "fixture_003",
      "source_file": "fixture_003_source.csv",
      "target_file": "fixture_003_target.csv",
      "injected_corruptions": [
        {
          "pattern_id": "timezone_shift",
          "params": {"offset_hours": 5.0, "affected_fraction": 0.3},
          "affected_rows": [12, 45, 67, ...],   // row indices in target
          "affected_column": "created_at"
        }
      ],
      "generation_seed": 42
    }

Key design point: `affected_rows` must be tracked precisely at
corruption time (not re-derived later by diffing) since that's the
ground truth for whether clustering correctly grouped the RIGHT rows
together, not just whether it guessed the right pattern name overall.
This lets scoring.py eventually measure cluster precision/recall
(did we group the right rows) separately from pattern-match
precision/recall (did we name the right cause) -- two different
failure modes worth distinguishing.
"""
