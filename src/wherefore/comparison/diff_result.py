"""
comparison/diff_result.py

Defines `DiffResult`, the canonical, normalized representation of "what
differs between source and target" that diff_engine.py produces and
clustering/cluster_mismatches.py consumes. This is the contract
boundary between the comparison engine and everything downstream --
clustering and the LLM never touch datacompy's raw output directly,
only this shape. That's what makes the underlying diff library
swappable later without touching clustering or reasoning.

Design note: this schema was designed AGAINST REAL datacompy 1.0.2
output, not speculated in advance (see the original stub's note about
an "open question" on dtype-mismatch vs. value-mismatch representation
-- resolved below, see DiffResult.column_summary).

What real datacompy.PandasCompare exposes that this wraps:
  - `compare.intersect_rows`: every joined row, source AND target value
    per column side by side, plus a `{column}_match` boolean per
    non-key column. This is the richest source available and is what
    `mismatches` (list[MismatchRow]) is built from -- it gives precise
    per-row, per-column mismatch detail, which neither `all_mismatch()`
    (row-level only) nor `column_stats` (column-level only) provide
    alone.
  - `compare.column_stats`: per-column dtype1/dtype2/unequal_cnt --
    this is where dtype mismatches are visible distinctly from
    value-level mismatches (a column can have dtype1 != dtype2 while
    individual values still "match" after coercion, or vice versa) --
    preserved as `ColumnSummary` below.
  - `compare.df1_unq_rows` / `compare.df2_unq_rows`: rows present in
    only one side -- preserved as `source_only_keys` / `target_only_keys`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class MismatchRow:
    """
    One column-level mismatch on one joined row. `key` supports
    composite keys (multiple join columns) by storing them as a dict,
    confirmed necessary since real-world datasets often need
    multi-column joins (e.g. region + id) -- datacompy itself supports
    this natively via join_columns=[...].
    """

    key: dict[str, Any]
    column: str
    source_value: Any
    target_value: Any


@dataclass
class ColumnSummary:
    """
    Per-column comparison summary, sourced from datacompy's
    column_stats. Kept distinct from MismatchRow because dtype
    mismatches and value mismatches are different signals that
    different taxonomy patterns care about: null_type_coercion cares
    about dtype1 != dtype2 even when individual values superficially
    match; float_precision cares about max_diff being small but
    nonzero across many rows in a column whose dtype matches on both
    sides.
    """

    column: str
    source_dtype: str
    target_dtype: str
    unequal_count: int
    match_count: int
    null_diff_count: int
    max_diff: float

    @property
    def dtype_mismatch(self) -> bool:
        return self.source_dtype != self.target_dtype

    @property
    def all_match(self) -> bool:
        return self.unequal_count == 0


@dataclass
class DiffResult:
    """
    The canonical, normalized diff output. Everything downstream
    (clustering, reasoning, report) consumes ONLY this shape -- never
    datacompy's raw Compare object directly.
    """

    join_columns: list[str]
    key_match_strategy: Literal["exact", "fuzzy"]

    source_row_count: int
    target_row_count: int
    matched_row_count: int

    # Rows present in only one side, identified by their key value(s).
    # Each entry is a dict mapping join column name -> value, e.g.
    # {"account_id": "ACCT-100042"} or {"region": "us", "id": 7} for
    # composite keys.
    source_only_keys: list[dict[str, Any]] = field(default_factory=list)
    target_only_keys: list[dict[str, Any]] = field(default_factory=list)

    column_summary: list[ColumnSummary] = field(default_factory=list)
    mismatches: list[MismatchRow] = field(default_factory=list)

    # Populated only when key_match_strategy == "fuzzy" -- maps a
    # stringified target key to the confidence score of its match to a
    # source key. Needed by the key_mismatch taxonomy pattern, which
    # specifically looks for LOW-confidence fuzzy matches as its signal.
    fuzzy_match_confidence: dict[str, float] | None = None

    def columns_with_mismatches(self) -> list[str]:
        return [c.column for c in self.column_summary if not c.all_match]

    def mismatches_for_column(self, column: str) -> list[MismatchRow]:
        return [m for m in self.mismatches if m.column == column]
