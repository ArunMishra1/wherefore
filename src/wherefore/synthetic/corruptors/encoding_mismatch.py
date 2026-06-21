"""
synthetic/corruptors/encoding_mismatch.py

Corrupts a string column by simulating the classic "UTF-8 text was
written, but the target system read it as Latin-1" migration bug --
the real mechanism that produces "mojibake" (e.g. "José" becoming
"JosÃ©"). This happens when an ETL step, database connection, or file
read declares the wrong character encoding for text that was actually
encoded differently.

Confirmed by direct testing: text.encode('utf-8').decode('latin-1')
reproduces real mojibake exactly, and the corruption is DETERMINISTICALLY
REVERSIBLE -- mojibake.encode('latin-1').decode('utf-8') recovers the
original text exactly. This reversibility is the actual mechanism the
detection signature checks directly (see
clustering/signatures.py's mojibake_reversible), rather than
approximating it with a regex over a hand-picked set of "suspicious"
characters.

Follows the same apply() contract as the other corruptors (see
CONTRIBUTING.md): takes a clean DataFrame, returns a corrupted copy
plus the exact affected row indices, computed at corruption time.
"""

from __future__ import annotations

import pandas as pd


def apply(
    df: pd.DataFrame,
    column: str,
    affected_fraction: float = 0.5,
    seed: int | None = None,
) -> tuple[pd.DataFrame, list[int]]:
    """
    Returns (corrupted_df, affected_row_indices).

    Selects `affected_fraction` of non-null rows at random (seeded)
    and re-encodes their value in `column` as UTF-8 bytes, then
    misreads those bytes as Latin-1 -- the real mojibake mechanism.

    A row only counts as genuinely affected if the mojibake transform
    actually CHANGED the value -- a value containing only plain ASCII
    characters is identical in UTF-8 and Latin-1, so corrupting it
    would be a no-op. Reporting an unaffected row as "affected" would
    make the ground truth lie about which rows the corruption actually
    touched, same principle as truncation.py's "already shorter than
    max_length" guard.

    Raises if `column` isn't a string-like dtype.
    """
    if not (df[column].dtype == "object" or str(df[column].dtype) in ("str", "string")):
        raise TypeError(
            f"encoding_mismatch.apply requires a string column, got dtype {df[column].dtype} for column {column!r}"
        )

    if not 0.0 < affected_fraction <= 1.0:
        raise ValueError(f"affected_fraction must be in (0, 1], got {affected_fraction}")

    corrupted = df.copy(deep=True)
    non_null_indices = corrupted.index[corrupted[column].notna()].tolist()

    if not non_null_indices:
        return corrupted, []

    n_selected = max(1, round(len(non_null_indices) * affected_fraction))
    selected_indices = sorted(
        pd.Series(non_null_indices).sample(n=n_selected, random_state=seed).tolist()
    )

    affected_row_indices = []
    for idx in selected_indices:
        original_value = str(corrupted.at[idx, column])
        try:
            mojibake_value = original_value.encode("utf-8").decode("latin-1")
        except UnicodeDecodeError:
            continue

        if mojibake_value != original_value:
            corrupted.at[idx, column] = mojibake_value
            affected_row_indices.append(idx)

    return corrupted, affected_row_indices
