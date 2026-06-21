"""
synthetic/corruptors/key_mismatch.py

Corrupts a dataset by reformatting a sample of rows' join keys on the
target side -- stripping the dash out of "EMP-1001" to get "EMP1001",
the realistic case where one system in a migration normalizes ID
formatting and the other doesn't. The row's CONTENT is otherwise
identical; only the key's literal string form changed.

This is the same real-world drift `--fuzzy-keys` (key_matching.py) is
built to resolve -- and for the common single-reformat case, it
usually does resolve it (dash-stripping alone scores ~93-95 with
rapidfuzz, comfortably above the 75 accept floor; see
key_matching.py's module docstring). What this corruptor exists to
test is the case BEFORE that resolution happens, or where it doesn't
run at all: from datacompy's perspective, a row with a reformatted key
is indistinguishable from a genuinely new/missing record -- it shows
up as a plain key mismatch, landing in source_only_rows on one side
and target_only_rows on the other, with nothing to suggest they're
actually the same underlying record.

Unlike every other corruptor in this taxonomy, key_mismatch's signal
does NOT show up in DiffResult.mismatches at all -- like
dedup_failure, it shows up entirely as unmatched rows (split across
BOTH source_only_rows and target_only_rows, not just one side, since
neither the original key nor the reformatted key has a literal partner
on the other side once they no longer match exactly). Detection
happens via clustering.cluster_mismatches.detect_row_presence_patterns,
the same row-presence path dedup_failure uses -- see that module's
docstring for the full architectural reasoning, and
clustering/signatures.py's key_format_similarity for the actual
detection logic.
"""

from __future__ import annotations

import pandas as pd


def _strip_separators(key: str) -> str:
    """The specific reformat this corruptor models: dashes and
    underscores removed, e.g. 'EMP-1001' -> 'EMP1001'. Confirmed by
    direct testing with rapidfuzz that this is exactly the realistic
    drift key_matching.py's module docstring describes and the
    --fuzzy-keys flag is designed to resolve."""
    return key.replace("-", "").replace("_", "")


def apply(
    df: pd.DataFrame,
    key_column: str,
    affected_fraction: float = 0.15,
    seed: int | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Returns (corrupted_df, original_keys_that_were_reformatted).

    Selects `affected_fraction` of rows at random (seeded) and
    rewrites their key_column value by stripping dashes/underscores
    via _strip_separators -- modeling a migration where the target
    system normalizes key formatting and the source doesn't (or vice
    versa; the direction doesn't matter to detection, which is
    symmetric across source_only/target_only).

    The returned list is the ORIGINAL (pre-reformat) key values, not
    the new ones -- this is what ground_truth.py needs to record as
    "these specific source-side keys are the ones whose target-side
    counterpart was reformatted out from under them."

    Raises if `key_column` isn't present, if affected_fraction would
    select zero rows, or if a selected key contains neither a dash nor
    an underscore (nothing for this corruptor to strip, which would
    silently produce a no-op "corruption" that's actually a clean
    exact match -- failing loudly here is better than a fixture that
    quietly doesn't test what it claims to).
    """
    if key_column not in df.columns:
        raise ValueError(f"key_column {key_column!r} not found in DataFrame columns")

    if not 0.0 < affected_fraction <= 1.0:
        raise ValueError(f"affected_fraction must be in (0, 1], got {affected_fraction}")

    n_to_affect = max(1, round(len(df) * affected_fraction))
    sampled = df.sample(n=n_to_affect, random_state=seed)

    original_keys = sampled[key_column].astype(str).tolist()
    for k in original_keys:
        if "-" not in k and "_" not in k:
            raise ValueError(
                f"key_column {key_column!r} value {k!r} has no '-' or '_' to strip; "
                "this corruptor needs a separator-bearing key format to model "
                "realistic reformatting drift, not a no-op."
            )

    corrupted = df.copy()
    corrupted.loc[sampled.index, key_column] = [
        _strip_separators(k) for k in original_keys
    ]

    return corrupted, original_keys
