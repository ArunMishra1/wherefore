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

Column-count / schema handling: PandasCompare's join and every
per-column stat it produces (column_stats, intersect_rows) operate
strictly on the INTERSECTION of source and target columns -- verified
directly against datacompy 1.0.2 (a column present on only one side
never appears in column_stats and is never compared, silently, with
no warning). Column ORDER, by contrast, is a non-issue: matching is
entirely name-keyed (confirmed directly -- swapping column order
between source and target produces identical dc.matches() /
intersect_rows output), so nothing here or downstream needs to
account for it.

What's NOT a non-issue is column PRESENCE: dc.df1_unq_columns() /
dc.df2_unq_columns() carry exactly the columns silently excluded from
comparison, and this function threads them into
DiffResult.source_only_columns/target_only_columns so that
information isn't lost. See DiffResult.has_schema_drift's docstring
for why this is reported as a plain structural fact rather than run
through clustering/taxonomy.
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

from wherefore.comparison.diff_result import ColumnSummary, DiffResult, MismatchRow, RowPresenceRecord


def compare(
    source: pd.DataFrame,
    target: pd.DataFrame,
    join_columns: str | list[str],
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
    fuzzy_match_confidence: dict[str, float] | None = None,
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

    `fuzzy_match_confidence` is purely a PASS-THROUGH: this function
    does not call key_matching.py itself, since by the time a caller
    has a DataFrame ready for compare(), fuzzy resolution (if any) has
    already happened and the DataFrames already reflect it. If a
    caller DID resolve fuzzy keys upstream (see cli.py's
    _apply_fuzzy_key_resolution), it should pass the resulting
    FuzzyMatchResult.confidence_by_target_key here so that signal isn't
    silently discarded -- it's exactly what the key_mismatch taxonomy
    pattern needs (see DiffResult.fuzzy_match_confidence's docstring).
    When provided (and non-empty), key_match_strategy is set to
    "fuzzy"; otherwise it stays "exact", matching prior behavior for
    every caller that doesn't pass this.
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
        key_match_strategy="fuzzy" if fuzzy_match_confidence else "exact",
        source_row_count=len(source),
        target_row_count=len(target),
        matched_row_count=len(dc.intersect_rows),
        source_only_keys=_extract_keys(dc.df1_unq_rows, join_columns),
        target_only_keys=_extract_keys(dc.df2_unq_rows, join_columns),
        source_only_rows=_extract_rows(dc.df1_unq_rows, join_columns),
        target_only_rows=_extract_rows(dc.df2_unq_rows, join_columns),
        column_summary=column_summary,
        mismatches=mismatches,
        # datacompy already computes this (df1_unq_columns/
        # df2_unq_columns) but it was previously discarded entirely --
        # column_stats (and everything built from it above) silently
        # only covers dc.intersect_columns(), so without this a
        # dropped/renamed column during migration produced NO signal
        # anywhere in DiffResult. Sorted for stable, diffable output
        # (datacompy returns an OrderedSet, whose iteration order
        # reflects insertion order from the source DataFrame's own
        # column order -- not wrong, just not a guarantee worth
        # depending on downstream).
        source_only_columns=sorted(dc.df1_unq_columns()),
        target_only_columns=sorted(dc.df2_unq_columns()),
        fuzzy_match_confidence=fuzzy_match_confidence,
    )


def _extract_keys(unique_rows_df: pd.DataFrame, join_columns: list[str]) -> list[dict]:
    """
    Converts datacompy's df1_unq_rows/df2_unq_rows (full-width
    DataFrames of rows present on only one side) into a list of plain
    key dicts, e.g. [{"account_id": "ACCT-100042"}], discarding the
    non-key columns -- callers needing the full row should re-look it
    up from the original source/target DataFrame using this key, or
    use _extract_rows instead, which carries the full row directly.
    """
    if len(unique_rows_df) == 0:
        return []
    return unique_rows_df[join_columns].to_dict(orient="records")


def _extract_rows(unique_rows_df: pd.DataFrame, join_columns: list[str]) -> list[RowPresenceRecord]:
    """
    Like _extract_keys, but keeps the full row (key + every other
    column's value), needed for dedup_failure/key_mismatch detection
    -- see RowPresenceRecord's docstring for why key-only isn't enough
    for those patterns.
    """
    if len(unique_rows_df) == 0:
        return []

    records = []
    for _, row in unique_rows_df.iterrows():
        key = {jc: row[jc] for jc in join_columns}
        values = {col: row[col] for col in unique_rows_df.columns if col not in join_columns}
        records.append(RowPresenceRecord(key=key, values=values))
    return records


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
    # dtype_mismatch_columns: columns where source_dtype != target_dtype.
    # Confirmed by direct testing that datacompy's per-row {col}_match
    # boolean is UNRELIABLE for EVERY row in these columns once the
    # overall column dtypes differ -- even rows whose individual cells
    # are still the exact same Python type and value on both sides
    # (e.g. a Timestamp next to the identical Timestamp) get reported
    # as mismatched, purely because OTHER cells in the column forced a
    # dtype change (e.g. one null coerced to a string sentinel forces
    # the whole column to object dtype).
    #
    # Fix: for these columns, compare each cell's (type, value) pair
    # directly rather than trusting datacompy's _match flag OR
    # comparing stringified values. String comparison was tried first
    # and rejected -- confirmed by direct testing it produces the WRONG
    # answer for a genuinely different-typed but identically-printed
    # case (float 10.5 vs str '10.5' must count as a real mismatch,
    # since a column actually changing type during migration is a real
    # finding, even when the printed values happen to look the same).
    # Comparing (type(value), value) tuples gets both cases right:
    # same type + same value -> not a mismatch (datacompy's bug,
    # suppressed); same printed value but different type -> still
    # correctly flagged as a mismatch.
    dtype_mismatch_columns = {
        stat["column"] for stat in compared_columns if stat["dtype1"] != stat["dtype2"]
    }

    def _cell_is_mismatch(source_value, target_value) -> bool:
        if type(source_value) is not type(target_value):
            return True
        try:
            return not (source_value == target_value)
        except TypeError:
            return True  # genuinely incomparable -- treat as a real mismatch, not a false negative

    mismatches: list[MismatchRow] = []
    for stat in compared_columns:
        col = stat["column"]
        source_col, target_col = f"{col}_source", f"{col}_target"
        if source_col not in intersect.columns or target_col not in intersect.columns:
            continue  # defensive: shouldn't happen given column_stats, but don't crash the whole diff over one odd column

        if col in dtype_mismatch_columns:
            candidate_rows = intersect
            row_is_mismatch = lambda row: _cell_is_mismatch(row[source_col], row[target_col])
        else:
            match_col = f"{col}_match"
            if match_col not in intersect.columns:
                continue
            candidate_rows = intersect[~intersect[match_col].astype(bool)]
            row_is_mismatch = lambda row: True  # already filtered by datacompy's flag

        for _, row in candidate_rows.iterrows():
            if not row_is_mismatch(row):
                continue
            key = {jc: row[jc] for jc in join_columns}
            mismatches.append(
                MismatchRow(key=key, column=col, source_value=row[source_col], target_value=row[target_col])
            )

    return mismatches
