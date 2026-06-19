"""
comparison/loaders.py

Loads CSV/JSON into normalized pandas DataFrames for the comparison
engine. The central design rule here, confirmed against real pandas
behavior rather than assumed: DO NOT let pandas' helpful defaults
silently erase the exact signals the taxonomy exists to detect.

Two concrete cases this resolves, both verified against real pandas
output before writing this module:

1. ENCODING. Reading a Latin-1-encoded file as UTF-8 raises
   UnicodeDecodeError -- it does not silently corrupt or guess. We
   keep that behavior: load_csv defaults to strict UTF-8 and lets the
   error surface, rather than catching it and falling back to another
   encoding automatically. Auto-fallback would hide exactly the signal
   encoding_mismatch needs to detect -- the FAILURE to decode under
   the expected encoding is itself diagnostic information, not
   something to paper over. Callers who genuinely have a non-UTF-8
   source file pass `encoding=` explicitly; that's a deliberate choice
   the caller makes, not a silent guess this module makes for them.

2. NULL REPRESENTATION. pandas' default read_csv treats "NULL", "NaN",
   "N/A", and an empty cell as the SAME null value -- verified: all
   four collapse to NaN by default. This destroys the distinction
   null_type_coercion needs (a column where target has the literal
   STRING "NULL" where source had a genuinely empty cell is a real
   migration bug pattern, not noise to be normalized away). We disable
   pandas' default null-string collapsing (`keep_default_na=False`)
   and treat ONLY a truly empty cell as null. Literal "NULL", "NaN",
   "N/A" strings are preserved as actual string values.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_csv(path: str | Path, encoding: str = "utf-8") -> pd.DataFrame:
    """
    Loads a CSV file. Only a truly empty cell is treated as null --
    literal strings like "NULL", "NaN", "N/A" are preserved as-is (see
    module docstring). Raises UnicodeDecodeError if the file isn't
    valid under `encoding` -- this is intentional, not a bug to catch.

    Also attempts to detect and parse datetime-looking string columns
    (see _try_parse_datetime_columns) -- CSV has no native datetime
    type, so without this, every datetime column round-trips through
    a CSV as plain strings, which silently breaks every downstream
    dtype-based pattern match (confirmed directly: a column that's a
    real datetime in memory becomes dtype 'str' after a CSV
    round-trip, and clustering's patterns_by_dtype then finds no
    candidates at all for it).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")

    df = pd.read_csv(
        path,
        encoding=encoding,
        keep_default_na=False,
        na_values=[""],
    )
    return _try_parse_datetime_columns(df)


def _try_parse_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    For each string/object column, attempts strict ISO8601 datetime
    parsing and converts the column if every non-null value parses
    successfully. Deliberately conservative:

    - Skips columns where every value is purely digits (e.g. "2024",
      "2025") -- confirmed directly that pd.to_datetime with
      format='ISO8601' WILL happily parse bare years as Jan 1st of
      that year, which would silently and wrongly convert a
      fiscal-year or birth-year column into fabricated timestamps.
      Genuine ISO8601 timestamps always contain a '-' or ':' separator,
      so requiring at least one non-digit character is a cheap,
      effective guard against this specific false positive.
    - Uses errors='raise' via a try/except, not errors='coerce' --
      coerce would silently turn unparseable values into NaT, which
      could convert a column that's mostly-but-not-entirely dates
      into something half-fabricated. Only convert when EVERY value
      parses cleanly.
    """
    df = df.copy()
    for col in df.columns:
        if df[col].dtype.name not in ("object", "str"):
            continue

        non_null = df[col].dropna()
        if len(non_null) == 0:
            continue
        if all(str(v).isdigit() for v in non_null):
            continue  # bare numeric strings (years, IDs) -- not a datetime column

        try:
            parsed = pd.to_datetime(df[col], errors="raise", format="ISO8601")
        except (ValueError, TypeError):
            continue

        df[col] = parsed

    return df


def load_json(path: str | Path) -> pd.DataFrame:
    """
    Loads a JSON file (array of flat objects) into a DataFrame.
    Unlike load_csv, JSON's `null` is unambiguous in the source format
    itself -- there's no "literal string NULL vs. empty string"
    ambiguity to resolve, since JSON distinguishes `null`, `""`, and
    the string `"null"` natively. pandas' json normalization respects
    that distinction already, so no special null handling is needed
    here the way it is for CSV.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")

    return pd.read_json(path)


def load_file(path: str | Path, encoding: str = "utf-8") -> pd.DataFrame:
    """
    Dispatches to load_csv or load_json based on file extension.
    Raises ValueError for unrecognized extensions rather than guessing
    a format -- guessing wrong silently would produce a confusingly
    malformed DataFrame rather than a clear error.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return load_csv(path, encoding=encoding)
    elif suffix == ".json":
        return load_json(path)
    else:
        raise ValueError(
            f"Unrecognized file extension {suffix!r} for {path}. "
            "wherefore currently supports .csv and .json."
        )
