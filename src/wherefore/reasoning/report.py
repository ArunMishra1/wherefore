"""
reasoning/report.py

NEXT TURN: implement this.

Purpose: takes the list of ClusterExplanation objects (one per
cluster) plus run metadata (dataset names, row counts, timestamp) and
renders the final Markdown report -- this is the `--output report.md`
deliverable from the CLI spec.

Report should structure roughly as:
  1. Summary: N rows compared, M mismatches found, K clusters
     identified, J of K matched to a known pattern
  2. Per-cluster section: pattern name (or "Unrecognized Pattern"),
     plain-English narrative, cited example rows (actual values, not
     just row indices -- per spec requirement), severity
  3. Any source-only / target-only rows (not column mismatches but
     missing/extra rows entirely) -- these may indicate key_mismatch or
     dedup_failure and should be presented even if no cluster narrative
     covers them

Keep this a pure rendering function (data in, Markdown string out) --
no I/O, no LLM calls -- so it's trivially testable against fixed
ClusterExplanation inputs without needing a real API call or files.
"""
