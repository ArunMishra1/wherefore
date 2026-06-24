"""
comparison/loaders.py

Loads CSV/JSON/Parquet/Excel into normalized pandas DataFrames for the
comparison engine, from either a local path or an s3:// URL. The
central design rule here, confirmed against real pandas behavior
rather than assumed: DO NOT let pandas' helpful defaults silently
erase the exact signals the taxonomy exists to detect.

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

S3 SUPPORT. `boto3` is an OPTIONAL dependency (`pip install
wherefore[s3]`), not a hard requirement -- most users never touch S3,
and the "lightweight" principle for this project means nobody pays for
boto3's install weight unless they actually use it. A real, confirmed
bug avoided here: Python's `pathlib.Path` silently MANGLES an s3://
URL (Path("s3://bucket/file.csv") collapses the double slash to
"s3:/bucket/file.csv", a corrupted path that still happens to keep a
correct-looking .suffix, masking the corruption). Every entry point in
this module checks for an s3:// prefix BEFORE ever constructing a
Path, routing S3 paths through `_fetch_s3_to_buffer` instead, which
downloads into an in-memory buffer that pandas' readers accept
natively (confirmed directly: pd.read_csv/read_parquet/read_excel all
accept BytesIO/StringIO, not just real paths).
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd


def _is_s3_path(path: str) -> bool:
    return isinstance(path, str) and path.startswith("s3://")


def _parse_s3_path(path: str) -> tuple[str, str]:
    """Splits 's3://bucket/key/with/slashes.csv' into (bucket, key)."""
    without_prefix = path[len("s3://"):]
    bucket, _, key = without_prefix.partition("/")
    if not bucket or not key:
        raise ValueError(f"Malformed S3 path: {path!r}. Expected s3://bucket/key.")
    return bucket, key


def _fetch_s3_to_buffer(path: str) -> io.BytesIO:
    """
    Downloads an S3 object into an in-memory BytesIO buffer. Requires
    boto3 (optional dependency: `pip install wherefore[s3]`) -- raises
    a clear, actionable ImportError if it's missing, rather than
    letting a raw "No module named 'boto3'" surface from deep inside
    pandas' call stack.

    Uses the standard AWS credential chain (env vars, ~/.aws/credentials,
    IAM role, AWS_PROFILE) via boto3's default behavior -- wherefore
    does not invent its own credential mechanism. NoCredentialsError
    and ClientError (e.g. bucket/key not found, access denied) are
    caught and re-raised with a clearer message; both are real,
    confirmed exception types from botocore, not guessed.
    """
    try:
        import boto3
        import botocore.exceptions
    except ImportError as e:
        raise ImportError(
            "Reading from S3 requires boto3, which is an optional dependency. "
            "Install it with: pip install wherefore[s3]"
        ) from e

    bucket, key = _parse_s3_path(path)
    client = boto3.client("s3")

    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except botocore.exceptions.NoCredentialsError as e:
        raise RuntimeError(
            "No AWS credentials found. wherefore uses the standard AWS credential "
            "chain (env vars AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
            "~/.aws/credentials, IAM role, or AWS_PROFILE) -- set one of these "
            "before reading from S3."
        ) from e
    except botocore.exceptions.ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        raise RuntimeError(
            f"Failed to read {path!r} from S3 (AWS error: {error_code}). "
            "Check the bucket/key are correct and your credentials have read access."
        ) from e

    return io.BytesIO(response["Body"].read())


def _resolve_source(path: str | Path) -> Path | io.BytesIO:
    """
    The single dispatch point every load_* function calls instead of
    constructing a Path directly. Returns the original path unchanged
    for local files (so existing Path-based behavior -- .exists()
    checks, suffix detection -- is completely undisturbed), or a
    downloaded in-memory buffer for s3:// paths.
    """
    if _is_s3_path(str(path)):
        return _fetch_s3_to_buffer(str(path))
    return Path(path)


def _suffix_from_path_string(path_str: str) -> str:
    """
    Extracts a file extension from a raw path string (including s3://
    URLs), e.g. '.csv' from 's3://bucket/exports/accounts.csv'.
    Returns '' if the filename has no dot. Operates on plain string
    splitting, never pathlib.Path, since Path() is confirmed to mangle
    s3:// URLs before this function would ever see them.
    """
    filename = path_str.rsplit("/", 1)[-1]
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


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

    Also accepts an s3:// path (see module docstring) -- the existence
    check below only applies to local paths; an S3 fetch that failed
    has already raised a clear error inside _resolve_source.
    """
    source = _resolve_source(path)
    if isinstance(source, Path) and not source.exists():
        raise FileNotFoundError(f"No such file: {source}")

    df = pd.read_csv(
        source,
        encoding=encoding,
        keep_default_na=False,
        na_values=[""],
    )
    return _try_parse_datetime_columns(df)


def _try_parse_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    For each string/object column that looks like it's mostly
    datetimes, parses the parseable values as real datetimes while
    PRESERVING THE ORIGINAL TEXT for any value that fails to parse --
    rather than either (a) requiring every value to parse (the
    original, stricter version of this function) or (b) coercing
    failures to NaT.

    This distinction matters concretely: a real migration bug this
    tool needs to detect is a genuine null being written as the
    literal string "NULL" in the target file, sitting among otherwise-
    real dates. Confirmed by direct testing that BOTH alternatives are
    wrong for this case:
      - errors="raise" (the original approach): the single "NULL"
        value blocks the ENTIRE column from being recognized as
        datetime, leaving every value -- including the 49 genuine
        dates -- as plain strings. Confirmed this caused the
        null_type_coercion pattern to be statistically undetectable
        once loaded from a real CSV file (diluted by ~49 spurious
        type-mismatch "mismatches" against the source file, which DID
        parse cleanly since it had no sentinel string).
      - errors="coerce": correctly parses the real dates, but ALSO
        silently turns "NULL" into NaT -- destroying the exact
        evidence null_type_coercion needs (a literal sentinel STRING
        next to a genuine null), making both sides look like ordinary
        matching nulls.

    The fix: parse with errors="coerce" to find out which values are
    genuinely parseable, then build a column with REAL datetimes where
    parsing succeeded and the ORIGINAL STRING preserved exactly where
    it failed. Gated by a failure-rate threshold (max 20% of non-null
    values may fail to parse) so a column that's mostly garbage isn't
    wrongly treated as "a date column with some sentinel values" --
    that threshold is a judgment call, not derived from a hard
    constraint; revisit if a real-world column shows this guard is
    too strict or too loose in practice.

    The bare-digit-years guard (e.g. "2024", "2025" parsing as
    Jan 1st of that year) is unchanged from the original version --
    confirmed directly that pd.to_datetime with format='ISO8601'
    parses bare numeric strings as dates, which would wrongly convert
    a fiscal-year/birth-year column.

    PERFORMANCE NOTE (added after real measurement, see PERFORMANCE.md):
    calling pd.to_datetime on the FULL column was confirmed to cost
    roughly as much as parsing the whole file in the first place --
    paid on every column, every load, even for columns with no
    plausible relationship to dates (e.g. "name_523891"-style
    strings). A cheap pre-check on a small RANDOM sample (not the
    first N rows -- confirmed directly that sentinel/null values can
    cluster early in a real export, which would make a first-N sample
    wrongly conclude a genuine date column has zero parseable values)
    rules out the common case -- a column that is obviously not
    dates -- using ~20 values instead of the full column. Confirmed
    directly: this sample check costs about 1/500th of the full-column
    call on a 1,000,000-row non-date column. The false-skip risk this
    introduces is real but vanishingly small: for a column sitting
    exactly at the existing 20%-failure-rate boundary, the probability
    a random 20-value sample shows zero successes (and so wrongly
    skips a column that would have passed the real check) is
    approximately 1 in 95 trillion -- computed directly, not assumed.
    This pre-check only ever SKIPS the expensive call early; it never
    changes the result for any column that proceeds past it.
    """
    MAX_PARSE_FAILURE_RATE = 0.2
    PRECHECK_SAMPLE_SIZE = 20

    df = df.copy()
    for col in df.columns:
        if df[col].dtype.name not in ("object", "str"):
            continue

        non_null_mask = df[col].notna()
        non_null = df[col][non_null_mask]
        if len(non_null) == 0:
            continue
        if all(str(v).isdigit() for v in non_null):
            continue  # bare numeric strings (years, IDs) -- not a datetime column

        if len(non_null) > PRECHECK_SAMPLE_SIZE:
            sample = non_null.sample(n=PRECHECK_SAMPLE_SIZE, random_state=0)
            sample_parsed = pd.to_datetime(sample, errors="coerce", format="ISO8601")
            if sample_parsed.isna().all():
                continue  # sample shows zero parseable values -- not a datetime column

        parsed = pd.to_datetime(df[col], errors="coerce", format="ISO8601")
        # A value parsed to NaT either because it genuinely failed to
        # parse, OR because it was already null in the original column
        # -- only the FORMER should be treated as a parse failure for
        # the threshold check and have its original text restored.
        parse_failed_mask = parsed.isna() & non_null_mask
        failure_rate = parse_failed_mask.sum() / len(non_null)

        if failure_rate == 0:
            df[col] = parsed
        elif failure_rate <= MAX_PARSE_FAILURE_RATE:
            hybrid = parsed.astype(object)
            hybrid[parse_failed_mask] = df[col][parse_failed_mask]
            df[col] = hybrid
        # else: failure rate too high -- leave the column as-is, it's
        # probably not actually a datetime column at all.

    return df


def load_parquet(path: str | Path) -> pd.DataFrame:
    """
    Loads a Parquet file. Unlike CSV, Parquet is a columnar format
    with NATIVE typing -- confirmed by direct testing that a real
    datetime column round-trips through Parquet as a real datetime
    dtype with no parsing step needed, sidestepping the entire class
    of CSV round-trip bugs this project hit twice (nanosecond-noise
    timestamps, the "NULL"-sentinel-blocks-the-whole-column datetime
    parsing failure). No special null handling is needed either --
    Parquet has a native null representation distinct from any string
    value, so there's no "literal NULL vs. empty cell" ambiguity to
    resolve the way there is for CSV.

    KNOWN LIMITATION, confirmed by direct testing: Parquet's columnar
    typing means a column CANNOT hold a mix of types (e.g. a real
    Timestamp next to the literal string "NULL") the way an in-memory
    pandas object-dtype column can -- writing such a column to Parquet
    raises pyarrow.lib.ArrowTypeError. This means null_type_coercion
    corruption is only representable in a Parquet file if the WHOLE
    column was already string-typed before the sentinel was
    introduced (e.g. a pipeline that stores timestamps as text) -- not
    on a column that's natively a Parquet timestamp/numeric type. This
    is an honest, real-world-accurate limitation: it reflects that
    Parquet's strong typing makes this specific failure mode
    genuinely less likely to occur in real Parquet-based pipelines in
    the first place, not a bug in this loader.
    Also accepts an s3:// path (see module docstring).
    """
    source = _resolve_source(path)
    if isinstance(source, Path) and not source.exists():
        raise FileNotFoundError(f"No such file: {source}")

    return pd.read_parquet(source)


def load_excel(path: str | Path, sheet_name: str | int = 0) -> pd.DataFrame:
    """
    Loads an Excel file (.xlsx). Confirmed by direct testing that
    pandas' default read_excel has the SAME null-collapsing behavior
    as read_csv -- a literal "NULL" string and a genuinely empty cell
    both collapse to NaN by default -- so the same fix applies:
    disable default null-string collapsing and treat only a truly
    empty cell as null.

    `sheet_name` defaults to the first sheet (index 0), matching
    pandas' own default -- exposed as a parameter since a real
    workbook may have multiple sheets and the caller may need a
    specific one, not silently always the first.
    Also accepts an s3:// path (see module docstring).
    """
    source = _resolve_source(path)
    if isinstance(source, Path) and not source.exists():
        raise FileNotFoundError(f"No such file: {source}")

    return pd.read_excel(
        source,
        sheet_name=sheet_name,
        keep_default_na=False,
        na_values=[""],
    )


def load_json(path: str | Path) -> pd.DataFrame:
    """
    Loads a JSON file (array of flat objects) into a DataFrame.
    Unlike load_csv, JSON's `null` is unambiguous in the source format
    itself -- there's no "literal string NULL vs. empty string"
    ambiguity to resolve, since JSON distinguishes `null`, `""`, and
    the string `"null"` natively. pandas' json normalization respects
    that distinction already, so no special null handling is needed
    here the way it is for CSV.
    Also accepts an s3:// path (see module docstring).
    """
    source = _resolve_source(path)
    if isinstance(source, Path) and not source.exists():
        raise FileNotFoundError(f"No such file: {source}")

    return pd.read_json(source)


def load_file(path: str | Path, encoding: str = "utf-8") -> pd.DataFrame:
    """
    Dispatches to the right loader based on file extension. Raises
    ValueError for unrecognized extensions rather than guessing a
    format -- guessing wrong silently would produce a confusingly
    malformed DataFrame rather than a clear error.

    Accepts an s3:// path alongside local paths. Confirmed by direct
    testing that Path("s3://bucket/file.csv") silently MANGLES the URL
    (collapses the double slash), so the suffix here is extracted from
    the raw path string directly for S3 paths -- never from a
    constructed Path -- and the original, unmangled string is passed
    through to the individual load_* functions, which resolve it via
    _resolve_source themselves.
    """
    path_str = str(path)
    if _is_s3_path(path_str):
        suffix = _suffix_from_path_string(path_str)
        file_for_loaders = path_str
    else:
        path_obj = Path(path)
        suffix = path_obj.suffix.lower()
        file_for_loaders = path_obj

    if suffix == ".csv":
        return load_csv(file_for_loaders, encoding=encoding)
    elif suffix == ".json":
        return load_json(file_for_loaders)
    elif suffix == ".parquet":
        return load_parquet(file_for_loaders)
    elif suffix in (".xlsx", ".xls"):
        return load_excel(file_for_loaders)
    else:
        raise ValueError(
            f"Unrecognized file extension {suffix!r} for {path}. "
            "wherefore currently supports .csv, .json, .parquet, and .xlsx/.xls."
        )
