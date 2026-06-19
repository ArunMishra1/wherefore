"""
comparison/diff_result.py

NEXT TURN: implement this.

Purpose: defines `DiffResult`, the canonical, normalized representation
of "what differs between source and target" that diff_engine.py produces
and clustering/cluster_mismatches.py consumes. This is the contract
boundary between the comparison engine and everything downstream --
clustering and the LLM never touch datacompy's raw output directly,
only this normalized shape. That's what makes it possible to swap the
underlying diff library later without touching clustering or reasoning.

Planned shape (subject to refinement once diff_engine.py is written
against real datacompy output):

    class MismatchRow(BaseModel):
        key: dict[str, Any]          # resolved join key(s) for this row
        column: str
        source_value: Any
        target_value: Any
        source_dtype: str
        target_dtype: str

    class DiffResult(BaseModel):
        matched_key_count: int
        source_only_keys: list[dict]   # rows in source, missing in target
        target_only_keys: list[dict]   # rows in target, missing in source
        mismatches: list[MismatchRow]
        key_match_strategy: Literal["exact", "fuzzy"]
        fuzzy_match_confidence: dict[str, float] | None  # if fuzzy used

Open question to resolve when implementing: how to represent
column-level dtype mismatches (e.g. source has int64, target has
object) vs. value-level mismatches within the same dtype -- these need
different downstream handling since a dtype mismatch is itself often
the root cause signal (see null_type_coercion pattern).
"""
