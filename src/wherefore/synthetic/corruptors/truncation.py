"""
synthetic/corruptors/truncation.py

Corrupts a string column by cutting values at a fixed character
length -- the classic "VARCHAR(255) was actually VARCHAR(50) on the
target system" migration bug, or a legacy fixed-width field truncating
silently on write.

Follows the same apply() contract as timezone_shift.py (see
CONTRIBUTING.md): takes a clean DataFrame, returns a corrupted copy
plus the exact affected row indices, computed at corruption time.
"""

from __future__ import annotations

import pandas as pd


def apply(
    df: pd.DataFrame,
    column: str,
    max_length: int = 20,
    affected_fraction: float = 0.3,
    seed: int | None = None,
) -> tuple[pd.DataFrame, list[int]]:
    """
    Returns (corrupted_df, affected_row_indices).

    Selects `affected_fraction` of rows at random (seeded) and cuts
    their value in `column` to the first `max_length` characters,
    leaving all other rows untouched.

    Only rows whose original value is actually LONGER than max_length
    count as truly corrupted and are included in affected_row_indices
    -- a row whose value is already shorter than max_length is
    unaffected by truncation regardless of whether it was selected,
    and reporting it as "affected" would make the ground truth lie
    about which rows the corruption actually touched.

    Raises if `column` isn't a string-like dtype -- truncating a
    non-string column doesn't model a real failure mode and would
    produce a ground-truth label that doesn't match what actually
    happened.
    """
    if not (df[column].dtype == "object" or str(df[column].dtype) in ("str", "string")):
        raise TypeError(
            f"truncation.apply requires a string column, got dtype {df[column].dtype} for column {column!r}"
        )

    if not 0.0 < affected_fraction <= 1.0:
        raise ValueError(f"affected_fraction must be in (0, 1], got {affected_fraction}")
    if max_length < 1:
        raise ValueError(f"max_length must be >= 1, got {max_length}")

    corrupted = df.copy(deep=True)
    n_rows = len(corrupted)
    n_selected = max(1, round(n_rows * affected_fraction))

    selected_indices = sorted(
        pd.Series(range(n_rows)).sample(n=n_selected, random_state=seed).tolist()
    )

    affected_row_indices = []
    for idx in selected_indices:
        original_value = corrupted.at[idx, column]
        if original_value is None or (isinstance(original_value, float)):
            continue  # null values can't be truncated
        if len(str(original_value)) > max_length:
            corrupted.at[idx, column] = str(original_value)[:max_length]
            affected_row_indices.append(idx)

    return corrupted, affected_row_indices
