"""
comparison/diff_engine.py

Thin wrapper around datacompy.PandasCompare that converts its output
into DiffResult. Deliberately stays thin -- if logic here grows beyond
"call datacompy, reshape the output," that logic belongs in
key_matching.py or clustering instead. See diff_result.py's module
docstring for exactly which datacompy attributes this reads and why.

Explicitly NOT this file's job: any pattern detection, clustering, or
causal reasoning. This is the line where "detecting THAT things
differ" ends and "explaining WHY" begins.
"""

from __future__ import annotations

import pandas as pd

try:
    import datacompy
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "datacompy is required for the comparison engine. "
        "Install it with: pip install datacompy"
    ) from e

from wherefore.comparison.diff_result import ColumnSummary, DiffResult, MismatchRow


def compare(
    source: pd.DataFrame,
    target: pd.DataFrame,
    join_columns: str | list[str],
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
) -> DiffResult:
    """
    Runs a datacompy comparison between source and target, joined on
    join_columns, and returns the normalized DiffResult.

    abs_tol / rel_tol are passed straight through to datacompy --
    useful for float_precision-adjacent cases where the caller already
    knows to tolerate tiny float rounding, though by default we use
    exact (0, 0) tolerance so float_precision drift is VISIBLE to
    clustering rather than silently absorbed here. Loosening tolerance
    is a caller decision, not a default -- silently swallowing small
    diffs would hide exactly the kind of pattern this tool exists to
    explain.

    Assumes exact key matching -- fuzzy key resolution happens in
    key_matching.py BEFORE this function is called; by the time a
    DataFrame reaches `compare()`, join_columns should already align
    exactly between source and target (key_matching.py is responsible
    for normalizing mismatched key formats upstream).
    """
    if isinstance(join_columns, str):
        join_columns = [join_columns]

    dc = datacompy.PandasCompare(
        source,
        target,
        join_columns=join_columns,
        abs_tol=abs_tol,
        rel_tol=rel_tol,
        df1_name="source",
        df2_name="target",
    )

    column_summary = [
        ColumnSummary(
            column=stat["column"],
            source_dtype=stat["dtype1"],
            target_dtype=stat["dtype2"],
            unequal_count=int(stat["unequal_cnt"]),
            match_count=int(stat["match_cnt"]),
            null_diff_count=int(stat["null_diff"]),
            max_diff=float(stat["max_diff"]),
        )
        for stat in dc.column_stats
        # join columns themselves aren't "compared" in the sense that
        # matters downstream -- they're definitionally equal by
        # construction of the join. Excluding them keeps column_summary
        # focused on columns where mismatches can actually occur.
        if stat["column"] not in join_columns
    ]

    mismatches = _extract_mismatches(dc, join_columns)

    return DiffResult(
        join_columns=join_columns,
        key_match_strategy="exact",
        source_row_count=len(source),
        target_row_count=len(target),
        matched_row_count=len(dc.intersect_rows),
        source_only_keys=_extract_keys(dc.df1_unq_rows, join_columns),
        target_only_keys=_extract_keys(dc.df2_unq_rows, join_columns),
        column_summary=column_summary,
        mismatches=mismatches,
    )


def _extract_keys(unique_rows_df: pd.DataFrame, join_columns: list[str]) -> list[dict]:
    """
    Converts datacompy's df1_unq_rows/df2_unq_rows (full-width
    DataFrames of rows present on only one side) into a list of plain
    key dicts, e.g. [{"account_id": "ACCT-100042"}], discarding the
    non-key columns -- callers needing the full row should re-look it
    up from the original source/target DataFrame using this key.
    """
    if len(unique_rows_df) == 0:
        return []
    return unique_rows_df[join_columns].to_dict(orient="records")


def _extract_mismatches(dc: "datacompy.PandasCompare", join_columns: list[str]) -> list[MismatchRow]:
    """
    Builds precise per-row, per-column MismatchRow records from
    dc.intersect_rows -- the only datacompy output that has both
    per-row AND per-column granularity simultaneously (see
    diff_result.py module docstring for why this is preferred over
    all_mismatch() or column_stats alone).

    intersect_rows columns are named `{col}_source`, `{col}_target`,
    and `{col}_match` for every non-key column, plus the join columns
    themselves unsuffixed.
    """
    intersect = dc.intersect_rows
    if len(intersect) == 0:
        return []

    compared_columns = [
        col for col in dc.column_stats if col["column"] not in join_columns
    ]

    mismatches: list[MismatchRow] = []
    for stat in compared_columns:
        col = stat["column"]
        match_col = f"{col}_match"
        if match_col not in intersect.columns:
            continue  # defensive: shouldn't happen given column_stats, but don't crash the whole diff over one odd column

        mismatched_rows = intersect[~intersect[match_col].astype(bool)]
        for _, row in mismatched_rows.iterrows():
            key = {jc: row[jc] for jc in join_columns}
            mismatches.append(
                MismatchRow(
                    key=key,
                    column=col,
                    source_value=row[f"{col}_source"],
                    target_value=row[f"{col}_target"],
                )
            )

    return mismatches
