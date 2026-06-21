"""
comparison/db.py

Database connectivity for wherefore -- the `db://table_name` source
type alongside local files and s3:// URLs. Implements the SourceSpec
abstraction sketched in TAXONOMY_TODO.md: a small, explicit way to say
"where does this data come from" that the rest of the pipeline
(comparison/clustering/taxonomy/explain) never has to know about --
the same separation that already exists between file formats via
loaders.py's dispatch-by-extension.

THIS ROUND ADDS POSTGRESQL, on top of the SQLite support built first
per the roadmap's own stated reasoning (SQLite needs no server, no
extra dependency, fully testable). Postgres needed a real server to
test honestly -- confirmed available in this environment via
py-pglite (a real PostgreSQL, compiled to WASM via PGlite, run through
a real psycopg2 connection -- not a mock or a stub), so Postgres
connectivity here is verified against an ACTUAL running Postgres
server, the same standard SQLite got, not assumed correct from
documentation alone. MySQL remains a real, tracked gap:
DatabaseBackend.MYSQL exists in the enum and is recognized by
parse_connection_string (connection-string FORMAT is generic across
all three backends, per explicit design decision with the user), but
query_table/list_columns/detect_primary_key/connect all raise
NotImplementedError for it rather than silently producing wrong
behavior or pretending to support a backend that was never actually
exercised against a real database.

CONNECTION STRING FORMAT, decided explicitly with the user rather than
guessed: a standard scheme://... URI, identical in shape to what
SQLAlchemy and most other Python DB tooling already use --
    sqlite:///relative/path/to/file.sqlite      (3 slashes: relative path)
    sqlite:////absolute/path/to/file.sqlite     (4 slashes: absolute path --
                                                  the 4th is the path's own
                                                  leading slash, confirmed
                                                  against SQLAlchemy's own
                                                  documented convention,
                                                  not assumed)
    postgresql://user:password@host:5432/dbname
    mysql://user:password@host:3306/dbname
This was chosen over alternatives (e.g. encoding the table name or
credentials directly in db://...) for one specific reason, confirmed
with the user: credentials must never appear in argv or shell history
(see CLI --source-conn-env/--target-conn-env, which point at an
ENVIRONMENT VARIABLE NAME holding one of these strings, never the
string itself). A bare table name on the command line (`db://accounts`)
combined with a connection string read from an env var keeps the CLI
argument identical across all three backends; only the env var's
VALUE changes per backend, which is the generic design the user asked
for.

PRIMARY KEY HANDLING, per the roadmap: auto-detected from the
database's own schema metadata (SQLite: PRAGMA table_info, confirmed
directly -- the 6th element of each row is a nonzero, sequential
"pk" position for primary-key columns, 0 otherwise. Postgres: a join
across pg_index/pg_attribute on indisprimary, confirmed directly
against a real Postgres server -- array_position(i.indkey, a.attnum)
gives the same sequential composite-key ordering SQLite's PRAGMA
gives for free, just via a real system-catalog join instead of a
single pragma call), so a composite key is fully recoverable on both
backends, not just "is there a PK at all". This module ONLY detects
and reports -- it never auto-applies a key without the caller (cli.py)
showing the user what was found and requiring explicit confirmation
first. That confirmation step is deliberately a CLI-layer concern, not
something baked into this module, since this module's job is "get the
data and the facts about it," not "decide whether it's safe to
proceed."

PANDAS + RAW PSYCOPG2 CONNECTION, a real, deliberate tradeoff:
pd.read_sql against a raw psycopg2 connection prints
"UserWarning: pandas only supports SQLAlchemy connectable... Other
DBAPI2 objects are not tested" -- confirmed by direct testing this
warning fires but the actual RESULTS are correct (NULL-vs-literal-string
distinction preserved, native TIMESTAMP columns round-trip as real
datetimes with no parsing step needed, unlike SQLite). Discussed
explicitly with the user: adding SQLAlchemy as a real dependency just
to silence an accurate-but-cosmetic warning was rejected in favor of
keeping the smaller dependency footprint, matching this project's
existing principle of not adding a library you don't strictly need
(see why urlparse, not SQLAlchemy, is used for connection-string
parsing above). The warning is suppressed explicitly in query_table
with a comment explaining why, not silently ignored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd


class DatabaseBackend(Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"


# Schemes recognized in a connection string, mapped to the backend they
# select. Multiple aliases for the same backend are listed because
# real-world connection strings in the wild use both interchangeably
# (confirmed: SQLAlchemy itself accepts both "postgresql://" and
# "postgres://" for the same backend) -- rejecting the alias a user
# is likely to actually type would be a needless rough edge.
_SCHEME_TO_BACKEND = {
    "sqlite": DatabaseBackend.SQLITE,
    "postgresql": DatabaseBackend.POSTGRESQL,
    "postgres": DatabaseBackend.POSTGRESQL,
    "mysql": DatabaseBackend.MYSQL,
}


def _is_db_source(source: str) -> bool:
    """True for the `db://table_name` CLI source syntax specifically
    -- NOT for a raw connection string (sqlite://, postgresql://,
    etc.), which is a different, separate string that only ever lives
    in an environment variable, never as the CLI source argument
    itself. Mirrors loaders.py's _is_s3_path in spirit: a cheap,
    unambiguous prefix check performed before any path/URL parsing
    that could otherwise mangle or misinterpret the string."""
    return isinstance(source, str) and source.startswith("db://")


def _table_name_from_db_source(source: str) -> str:
    """Extracts 'accounts' from 'db://accounts'. Deliberately strict:
    raises ValueError on anything that isn't a single, non-empty
    table-name segment (no slashes, no query string) -- a malformed
    db:// source should fail loudly and immediately here, not produce
    a confusing downstream SQL error from a mangled table name."""
    without_prefix = source[len("db://"):]
    if not without_prefix or "/" in without_prefix or "?" in without_prefix:
        raise ValueError(
            f"Malformed db:// source: {source!r}. Expected db://table_name "
            "(a single table name, no slashes or query parameters)."
        )
    return without_prefix


@dataclass(frozen=True)
class ConnectionInfo:
    """
    Parsed result of a connection-string URI. Deliberately a plain,
    inspectable dataclass rather than a live connection object --
    parsing and connecting are kept as two separate steps so the
    parsed form can be validated, logged, or shown to the user (e.g.
    "connecting to sqlite database at <path>") without ever needing to
    actually open a connection first.

    `password` is intentionally part of this object (so a real
    connection can be opened from it) but callers must NEVER log,
    print, or include this dataclass's repr in any user-facing output
    -- see __repr__ override below, which masks it unconditionally,
    since a dataclass's default repr would otherwise print every
    field verbatim and defeat the entire point of keeping credentials
    out of logs/terminal history.
    """

    backend: DatabaseBackend
    # SQLite-specific: absolute or relative filesystem path to the .sqlite file.
    sqlite_path: str | None = None
    # Postgres/MySQL-specific (parsed now for forward-compatibility,
    # per the generic-from-day-one decision; unused until those
    # backends are actually implemented):
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    # Postgres/MySQL-specific: connection-string query parameters
    # (e.g. ?sslmode=disable), passed through to the driver's connect
    # call. Found to be a REAL need, not a speculative one: confirmed
    # while testing against PGlite's TCP mode, which requires
    # sslmode=disable since its minimal TCP server doesn't correctly
    # implement Postgres's SSL negotiation handshake -- but real-world
    # managed/cloud Postgres and local dev setups commonly need
    # sslmode (or other options) specified too, for genuinely
    # different reasons (cert verification, self-signed certs, etc.).
    # Default empty dict, never None, so callers can always iterate it
    # without a null check.
    options: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        # Never include password in any repr -- this object may end
        # up in a traceback, a debug log, or an error message, and a
        # leaked credential there is exactly the failure mode the
        # whole env-var design exists to prevent.
        if self.backend is DatabaseBackend.SQLITE:
            return f"ConnectionInfo(backend=sqlite, sqlite_path={self.sqlite_path!r})"
        return (
            f"ConnectionInfo(backend={self.backend.value}, host={self.host!r}, "
            f"port={self.port!r}, database={self.database!r}, "
            f"username={self.username!r}, password=<redacted>, "
            f"options={self.options!r})"
        )


def parse_connection_string(conn_str: str) -> ConnectionInfo:
    """
    Parses a connection-string URI (read from an environment variable
    by the CLI layer -- see module docstring) into a ConnectionInfo.

    SQLite is handled as a SPECIAL CASE, bypassing urlparse entirely
    for the path itself -- confirmed by direct testing that
    urllib.parse.urlparse's netloc/path split does NOT match
    SQLAlchemy's real, documented sqlite:/// (relative) vs
    sqlite:////  (absolute) convention: urlparse always reads the
    first slash after the empty netloc as part of `.path`, which
    means a 4-slash absolute-path string comes back from urlparse as
    "//absolute/path" (a doubled leading slash, not a clean one) and a
    3-slash relative-path string comes back as "/relative/path" (a
    spurious leading slash on something meant to be relative) --
    neither is usable as-is. The correct, verified approach is a
    literal string-prefix strip of exactly "sqlite:///" (3 slashes):
    whatever remains IS the path exactly as the user intended it,
    since a 4th slash the user typed for an absolute path simply
    survives untouched in what's left after stripping only 3.

    Non-SQLite schemes (Postgres/MySQL) don't have this ambiguity --
    they have a real host/port/database structure urlparse parses
    correctly -- so they go through the normal urlparse path.

    Raises ValueError for an unrecognized scheme or a malformed
    SQLite path -- never silently guesses a backend.
    """
    parsed = urlparse(conn_str)
    scheme = parsed.scheme.lower()

    if scheme not in _SCHEME_TO_BACKEND:
        raise ValueError(
            f"Unrecognized database scheme {scheme!r} in connection string. "
            f"Supported: {', '.join(sorted(s for s in _SCHEME_TO_BACKEND))}."
        )
    backend = _SCHEME_TO_BACKEND[scheme]

    if backend is DatabaseBackend.SQLITE:
        sqlite_path = _parse_sqlite_path(conn_str)
        return ConnectionInfo(backend=backend, sqlite_path=sqlite_path)

    # Postgres/MySQL: urlparse's netloc/path split is correct for
    # these (real host-based URLs, no SQLite-specific ambiguity).
    # Query string (e.g. ?sslmode=disable) is parsed into `options` --
    # found to be a real need, not speculative, while testing against
    # PGlite's TCP mode (see ConnectionInfo.options' docstring).
    query_options = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    return ConnectionInfo(
        backend=backend,
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.lstrip("/") or None,
        username=parsed.username,
        password=parsed.password,
        options=query_options,
    )


def _parse_sqlite_path(conn_str: str) -> str:
    """
    Extracts the filesystem path from a sqlite:// connection string,
    via a literal string-prefix strip rather than urlparse (see
    parse_connection_string's docstring for why urlparse's netloc/path
    split doesn't work correctly for this specific scheme).

    Required prefix is exactly "sqlite:///" (three slashes) --
    confirmed against SQLAlchemy's own documented convention, the de
    facto standard for this URI shape:
        sqlite:///relative/path.sqlite     -> "relative/path.sqlite"
        sqlite:////absolute/path.sqlite    -> "/absolute/path.sqlite"
                                               (the 4th slash IS the
                                               path's own leading slash,
                                               which simply survives
                                               the 3-slash prefix strip
                                               untouched)
    Verified directly: stripping exactly "sqlite:///" and nothing more
    produces the correct result in both cases -- confirmed by testing
    all three real shapes (relative, absolute, and a path that merely
    LOOKS absolute-ish like "tmp/foo.sqlite" with no leading slash at
    all, which is still correctly treated as relative).
    """
    prefix = "sqlite:///"
    if not conn_str.startswith(prefix):
        raise ValueError(
            f"Malformed sqlite connection string: {conn_str!r}. Expected at least "
            "'sqlite:///' (three slashes) followed by a path -- "
            "sqlite:///relative/path.sqlite for a relative path, or "
            "sqlite:////absolute/path.sqlite for an absolute one (note the 4th "
            "slash there is the path's own leading slash, not an extra delimiter)."
        )
    path = conn_str[len(prefix):]
    if not path:
        raise ValueError(
            f"Malformed sqlite connection string: {conn_str!r}. No file path found "
            "after 'sqlite:///'."
        )
    return path


def connect(info: ConnectionInfo):
    """
    Opens a real connection for the given ConnectionInfo.

    SQLite uses Python's stdlib sqlite3 -- no optional dependency.
    Postgres uses psycopg2, an OPTIONAL dependency (matching the
    pattern loaders.py already established for boto3/S3): raises a
    clear, actionable ImportError if it's missing, rather than letting
    a raw "No module named 'psycopg2'" surface from deep inside this
    function's call stack. info.options (parsed from the connection
    string's query parameters, e.g. ?sslmode=disable) is passed
    through as additional psycopg2.connect() keyword arguments --
    confirmed this is a real need, not speculative, while testing
    against PGlite's TCP mode, which requires sslmode=disable since
    its minimal TCP implementation doesn't correctly negotiate
    Postgres's SSL handshake; real managed/cloud Postgres deployments
    have genuinely different reasons to need sslmode or other options
    specified too.

    MySQL raises NotImplementedError with a clear message rather than
    attempting an import that would fail confusingly, or (worse)
    silently returning something that looks like a connection but
    isn't.
    """
    if info.backend is DatabaseBackend.SQLITE:
        path = Path(info.sqlite_path)
        if not path.exists():
            raise FileNotFoundError(
                f"No such SQLite database file: {info.sqlite_path}"
            )
        return sqlite3.connect(str(path))

    if info.backend is DatabaseBackend.POSTGRESQL:
        try:
            import psycopg2
        except ImportError as e:
            raise ImportError(
                "Connecting to PostgreSQL requires psycopg2, which is an optional "
                "dependency. Install it with: pip install wherefore[db]"
            ) from e

        try:
            return psycopg2.connect(
                host=info.host,
                port=info.port,
                dbname=info.database,
                user=info.username,
                password=info.password,
                **info.options,
            )
        except psycopg2.OperationalError as e:
            # Deliberately does NOT include the password in this message
            # -- psycopg2's own OperationalError text can include the
            # connection parameters it tried, which would leak the
            # password into an error message/log. Re-raised with a
            # clean, credential-free message instead.
            raise RuntimeError(
                f"Failed to connect to PostgreSQL at {info.host}:{info.port}/"
                f"{info.database}. Check the host/port/database are correct and "
                "your credentials are valid. (Original error type: "
                f"{type(e).__name__})"
            ) from e

    raise NotImplementedError(
        f"{info.backend.value} connectivity is not yet implemented. "
        "Currently sqlite:// and postgresql:// connection strings are supported. "
        "(This is real, tracked future work -- see TAXONOMY_TODO.md -- "
        "not a silent gap.)"
    )


def list_columns(conn, table_name: str) -> list[str]:
    """
    Returns the real column names for `table_name`, in schema order.
    Used by cli.py to validate that a --key the user passed explicitly
    actually exists in the table BEFORE running a query against it,
    the same fail-fast principle loaders.py applies to file paths.

    SQLite: PRAGMA table_info. Postgres: information_schema.columns,
    confirmed by direct testing against a real Postgres server that
    this correctly returns columns in schema order and an EMPTY list
    (not an error) for a nonexistent table -- this function checks for
    that emptiness explicitly and raises its own clear ValueError,
    since Postgres itself stays silent about it.
    """
    if isinstance(conn, sqlite3.Connection):
        rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table_name)})").fetchall()
        if not rows:
            raise ValueError(f"Table {table_name!r} not found in this database.")
        return [row[1] for row in rows]

    import psycopg2

    if isinstance(conn, psycopg2.extensions.connection):
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (table_name,),
        )
        rows = cur.fetchall()
        if not rows:
            raise ValueError(f"Table {table_name!r} not found in this database.")
        return [row[0] for row in rows]

    raise NotImplementedError(
        "list_columns is only implemented for SQLite and PostgreSQL connections."
    )


def detect_primary_key(conn, table_name: str) -> list[str] | None:
    """
    Returns the real primary key column(s) for `table_name`, reading
    the database's own schema metadata -- NOT a heuristic guess the
    way cli.py's _auto_detect_key is for files (uniqueness ratio,
    "id"/"key" in the name). A database genuinely KNOWS its primary
    key; this function reports that fact rather than re-deriving an
    approximation of it.

    SQLite: PRAGMA table_info's 6th element ("pk") is 0 for non-key
    columns, and a nonzero, sequential POSITION (1, 2, 3...) for
    primary-key columns -- not just a boolean -- so a composite
    primary key is correctly recoverable in its real column order.

    Postgres: a join across pg_index/pg_attribute filtered on
    indisprimary, confirmed directly against a real Postgres server
    (via py-pglite, a real PostgreSQL run through a genuine psycopg2
    connection -- not mocked). array_position(i.indkey, a.attnum)
    recovers the same sequential composite-key ordering SQLite's
    PRAGMA gives for free; confirmed this returns columns in the
    correct key order on a real composite-PK table, and an empty list
    (not an error) for a table with no primary key at all -- this
    function maps that empty-list case to None explicitly, matching
    SQLite's contract.

    Returns None if the table has no primary key at all -- callers
    (cli.py) must decide what to do with that; this function only
    reports the schema fact, it never falls back to guessing a
    substitute key the way the file-based heuristic does, since a
    guessed key against a live database is exactly the higher-stakes
    mistake the roadmap calls out as needing real confirmation, not a
    silent substitute.
    """
    if isinstance(conn, sqlite3.Connection):
        rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table_name)})").fetchall()
        if not rows:
            raise ValueError(f"Table {table_name!r} not found in this database.")
        pk_columns = [(row[5], row[1]) for row in rows if row[5] > 0]
        if not pk_columns:
            return None
        pk_columns.sort(key=lambda pair: pair[0])  # order by PK position, not column order
        return [name for _, name in pk_columns]

    import psycopg2

    if isinstance(conn, psycopg2.extensions.connection):
        # Confirm the table exists at all first -- list_columns already
        # does this check, reused here rather than duplicating the
        # information_schema query, so "table not found" is reported
        # identically regardless of which function the caller used first.
        list_columns(conn, table_name)

        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = %s::regclass AND i.indisprimary
            ORDER BY array_position(i.indkey, a.attnum)
            """,
            (table_name,),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        return [row[0] for row in rows]

    raise NotImplementedError(
        "detect_primary_key is only implemented for SQLite and PostgreSQL connections."
    )


def query_table(conn, table_name: str) -> pd.DataFrame:
    """
    Loads an entire table into a DataFrame via pandas.read_sql.

    SQLite: confirmed by direct testing against a real SQLite in-memory
    database that pd.read_sql already preserves a literal string like
    "NULL" as distinct from a genuine SQL NULL (which becomes a real
    NaN) -- SQLite's native per-VALUE typing means this comes for free.
    SQLite has no native datetime type (everything is stored as TEXT,
    INTEGER, or REAL) -- confirmed directly that a datetime column
    round-trips through SQLite as plain TEXT, the identical problem CSV
    has. Reuses loaders.py's _try_parse_datetime_columns (not a
    reimplementation) for exactly this reason.

    Postgres: confirmed by direct testing against a real Postgres
    server (via py-pglite) that BOTH of the above come for free here
    too, and even more completely than SQLite -- a native TIMESTAMP
    column round-trips as a real pandas datetime64 dtype directly, with
    NO parsing step needed at all (closer to Parquet's behavior than
    SQLite's). _try_parse_datetime_columns is correspondingly NOT
    called for Postgres -- there's nothing TEXT-typed to fix.

    pd.read_sql against a raw psycopg2 connection (not a SQLAlchemy
    engine) prints "UserWarning: pandas only supports SQLAlchemy
    connectable... Other DBAPI2 objects are not tested." Confirmed by
    direct testing this warning is accurate about being unsupported/
    untested by pandas upstream, but NOT accurate about producing wrong
    results here -- explicitly verified correct NULL handling and
    datetime typing despite the warning. Discussed with the user:
    adding SQLAlchemy as a dependency just to silence this was
    rejected in favor of the smaller footprint (see module docstring).
    The warning is suppressed explicitly here, scoped to only this one
    call, with this comment as the record of why -- not silently
    swallowed without explanation.
    """
    if isinstance(conn, sqlite3.Connection):
        from wherefore.comparison.loaders import _try_parse_datetime_columns

        df = pd.read_sql(f"SELECT * FROM {_quote_identifier(table_name)}", conn)
        return _try_parse_datetime_columns(df)

    import psycopg2

    if isinstance(conn, psycopg2.extensions.connection):
        import warnings

        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="pandas only supports SQLAlchemy connectable",
                category=UserWarning,
            )
            return pd.read_sql(f"SELECT * FROM {_quote_identifier(table_name)}", conn)

    raise NotImplementedError(
        "query_table is only implemented for SQLite and PostgreSQL connections."
    )


def _quote_identifier(name: str) -> str:
    """
    Wraps a table name in double quotes, the standard SQL identifier
    quoting convention -- confirmed by direct testing that BOTH SQLite
    and Postgres accept double-quoted identifiers identically (an
    earlier version of this docstring incorrectly called this
    SQLite-specific before Postgres support was added and this was
    actually verified against a real Postgres server). MySQL's default
    convention uses backticks instead -- not yet relevant since MySQL
    isn't implemented in this module at all yet.

    This exists to correctly support table names that are themselves
    SQL keywords or contain special characters, NOT as an attempt at
    general SQL-injection defense -- the table name here always comes
    from the CLI's own db:// argument (already validated by
    _table_name_from_db_source to contain no slashes or query
    characters), never from untrusted external input passed through
    at query time.
    """
    escaped = name.replace('"', '""')
    return f'"{escaped}"'
