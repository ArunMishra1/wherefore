"""
synthetic/base_dataset.py

NEXT TURN: implement this.

Purpose: generate realistic-looking "clean" tabular data (think:
customer records, transaction logs, order tables -- domains with
natural datetime, string, numeric, and enum/lookup columns) that
serves as the SOURCE dataset before any corruption is applied. The
target dataset is a copy of this with one or more corruptors from
synthetic/corruptors/ applied to it.

Needs enough column variety to give every taxonomy pattern something
realistic to corrupt: at least one datetime column (timezone_shift),
string columns with non-ASCII content (encoding_mismatch), nullable
columns (null_type_coercion), a natural key + some near-duplicate rows
(dedup_failure), float columns with meaningful precision (float_precision),
and a categorical/enum column with a fixed value set (enum_drift).

Should be deterministic given a seed (numpy/random seed param) since
committed fixtures (per project decision: commit everything generated)
need to be exactly reproducible by anyone running the regenerate script.
"""
