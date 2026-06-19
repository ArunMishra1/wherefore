"""
synthetic/corruptors/timezone_shift.py

NEXT TURN: implement this.

Reference corruptor -- the import path
"wherefore.synthetic.corruptors.timezone_shift:apply" in
taxonomy/patterns/timezone_shift.yaml points HERE. This file's
function signature is therefore part of the taxonomy contract: every
other corruptor module should follow the same `apply()` signature so
ground_truth.py and the regenerate script can call any corruptor
uniformly via taxonomy.registry.resolve_import_path().

Planned signature:

    def apply(
        df: pd.DataFrame,
        column: str,
        offset_hours: float = 5.0,
        affected_fraction: float = 0.3,
        seed: int | None = None,
    ) -> tuple[pd.DataFrame, list[int]]:
        '''
        Returns (corrupted_df, affected_row_indices).
        affected_row_indices is REQUIRED, not optional -- it's written
        directly into ground_truth.json by ground_truth.py and is the
        precise eval target for cluster-level (not just pattern-level)
        scoring. Computing it accurately at corruption time, rather
        than inferring it later by diffing, is what keeps the ground
        truth actually trustworthy.
        '''

Implementation sketch: pick `affected_fraction` of rows at random
(seeded), add `offset_hours` to the target's datetime value in
`column` for those rows only, leave the rest untouched. This produces
exactly the "constant_offset_subset" statistical signature that
clustering/signatures.py needs to detect.
"""
