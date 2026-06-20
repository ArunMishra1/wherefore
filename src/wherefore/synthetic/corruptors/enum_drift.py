"""
synthetic/corruptors/enum_drift.py

Corrupts an enum/categorical column by applying a consistent
value-remapping to a subset of rows -- the classic "the old system
called it 'approved', the new system calls it 'APPROVED'" migration
bug, or a status-code remapping where '1' becomes 'Active' on the
new side. The defining trait of this failure mode (as opposed to
random data corruption) is that the SAME source value always maps to
the SAME target value -- it's a systematic recode, not noise.

Follows the same apply() contract as timezone_shift.py and
truncation.py (see CONTRIBUTING.md).
"""

from __future__ import annotations

import pandas as pd


def apply(
    df: pd.DataFrame,
    column: str,
    value_mapping: dict[str, str],
    affected_fraction: float = 0.3,
    seed: int | None = None,
) -> tuple[pd.DataFrame, list[int]]:
    """
    Returns (corrupted_df, affected_row_indices).

    `value_mapping` defines the recode, e.g. {"approved": "APPROVED",
    "denied": "REJECTED"} -- only rows whose current value is a KEY in
    value_mapping are eligible for corruption; rows with a value not
    in the mapping are left untouched regardless of selection, since
    there's nothing to remap them to.

    Selects `affected_fraction` of the ELIGIBLE rows (not all rows) at
    random (seeded) and applies the mapping to them, leaving the rest
    -- including other eligible rows that weren't selected -- as-is.
    This models a partial migration: some records went through the
    new recoding logic, some didn't (e.g. a batch job that was
    interrupted, or two parallel write paths where only one was updated).

    Raises if `column` isn't string-like, or if value_mapping is empty.
    """
    if not (df[column].dtype == "object" or str(df[column].dtype) in ("str", "string")):
        raise TypeError(
            f"enum_drift.apply requires a string column, got dtype {df[column].dtype} for column {column!r}"
        )
    if not value_mapping:
        raise ValueError("value_mapping must not be empty")
    if not 0.0 < affected_fraction <= 1.0:
        raise ValueError(f"affected_fraction must be in (0, 1], got {affected_fraction}")

    corrupted = df.copy(deep=True)

    eligible_indices = corrupted.index[corrupted[column].isin(value_mapping.keys())].tolist()
    if not eligible_indices:
        return corrupted, []

    n_selected = max(1, round(len(eligible_indices) * affected_fraction))
    selected_indices = sorted(
        pd.Series(eligible_indices).sample(n=n_selected, random_state=seed).tolist()
    )

    for idx in selected_indices:
        old_value = corrupted.at[idx, column]
        corrupted.at[idx, column] = value_mapping[old_value]

    return corrupted, selected_indices
