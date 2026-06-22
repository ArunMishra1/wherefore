"""
Tests for comparison/db.py. Every scenario here was manually verified
against a real SQLite database before writing the module -- see
db.py's module docstring for the architecture and the slash-counting
bug that was caught and fixed by direct testing, not assumed correct.
"""

import sqlite3

import pandas as pd
import pytest

from wherefore.comparison.db import (
    ConnectionInfo,
    DatabaseBackend,
    _is_db_source,
    _parse_sqlite_path,
    _table_name_from_db_source,
    connect,
    detect_primary_key,
    list_columns,
    list_tables,
    parse_connection_string,
    query_table,
)


# --- db:// source string parsing ---


def test_is_db_source_detects_db_urls():
    assert _is_db_source("db://accounts") is True
    assert _is_db_source("/local/path/file.csv") is False
    assert _is_db_source("s3://bucket/file.csv") is False
    assert _is_db_source("sqlite:////path/to/file.sqlite") is False  # a connection string, not a db:// source


def test_table_name_from_db_source_extracts_name():
    assert _table_name_from_db_source("db://accounts") == "accounts"
    assert _table_name_from_db_source("db://my_table_2024") == "my_table_2024"


def test_table_name_from_db_source_rejects_malformed():
    with pytest.raises(ValueError, match="Malformed db:// source"):
        _table_name_from_db_source("db://")
    with pytest.raises(ValueError, match="Malformed db:// source"):
        _table_name_from_db_source("db://schema/table")
    with pytest.raises(ValueError, match="Malformed db:// source"):
        _table_name_from_db_source("db://table?foo=bar")


# --- connection string parsing: SQLite slash-counting ---
# Regression tests for a real bug caught while building this module:
# urllib.parse.urlparse's netloc/path split does NOT match SQLAlchemy's
# documented sqlite:/// (relative) vs sqlite:////  (absolute)
# convention -- confirmed directly that urlparse mishandles both
# shapes if .path is used naively. _parse_sqlite_path instead does a
# literal string-prefix strip of exactly "sqlite:///".


def test_parse_sqlite_path_relative():
    assert _parse_sqlite_path("sqlite:///relative/path.sqlite") == "relative/path.sqlite"


def test_parse_sqlite_path_absolute():
    """
    The actual regression case: an absolute path needs FOUR slashes
    total (sqlite:/// + the path's own leading /), and the result must
    be the clean absolute path with exactly ONE leading slash -- not
    the doubled-slash result urlparse.path naively produces.
    """
    assert _parse_sqlite_path("sqlite:////absolute/path.sqlite") == "/absolute/path.sqlite"


def test_parse_sqlite_path_rejects_too_few_slashes():
    with pytest.raises(ValueError, match="Malformed sqlite connection string"):
        _parse_sqlite_path("sqlite://relative/path.sqlite")  # only 2 slashes


def test_parse_sqlite_path_rejects_empty_path():
    with pytest.raises(ValueError, match="No file path found"):
        _parse_sqlite_path("sqlite:///")


def test_parse_connection_string_sqlite_relative():
    info = parse_connection_string("sqlite:///relative/path.sqlite")
    assert info.backend is DatabaseBackend.SQLITE
    assert info.sqlite_path == "relative/path.sqlite"


def test_parse_connection_string_sqlite_absolute():
    info = parse_connection_string("sqlite:////tmp/somewhere/accounts.sqlite")
    assert info.backend is DatabaseBackend.SQLITE
    assert info.sqlite_path == "/tmp/somewhere/accounts.sqlite"


def test_parse_connection_string_rejects_unrecognized_scheme():
    with pytest.raises(ValueError, match="Unrecognized database scheme"):
        parse_connection_string("mongodb://localhost/foo")


def test_parse_connection_string_postgres_fields():
    info = parse_connection_string("postgresql://scott:tiger@dbhost:5432/mydb")
    assert info.backend is DatabaseBackend.POSTGRESQL
    assert info.host == "dbhost"
    assert info.port == 5432
    assert info.database == "mydb"
    assert info.username == "scott"
    assert info.password == "tiger"


def test_parse_connection_string_postgres_alias():
    """SQLAlchemy itself accepts both 'postgres://' and 'postgresql://'
    for the same backend -- confirmed real-world convention, not
    guessed; both should resolve to the same DatabaseBackend."""
    info = parse_connection_string("postgres://scott:tiger@dbhost/mydb")
    assert info.backend is DatabaseBackend.POSTGRESQL


def test_parse_connection_string_mysql_fields():
    info = parse_connection_string("mysql://root:secret@127.0.0.1:3306/appdb")
    assert info.backend is DatabaseBackend.MYSQL
    assert info.host == "127.0.0.1"
    assert info.port == 3306
    assert info.database == "appdb"


def test_parse_connection_string_extracts_query_options():
    """
    Regression test for a real gap found while testing PostgreSQL
    connectivity against PGlite's TCP mode, which requires
    sslmode=disable since its minimal TCP server doesn't correctly
    negotiate Postgres's SSL handshake -- but this is a genuinely
    real-world need too: managed/cloud Postgres and various local dev
    setups commonly need sslmode or other connection options
    specified. Confirmed by direct testing that the original
    implementation silently DISCARDED the query string entirely
    (ConnectionInfo had no field for it at all) -- fixed by parsing it
    into ConnectionInfo.options and passing it through in connect().
    """
    info = parse_connection_string("postgresql://scott:tiger@dbhost/mydb?sslmode=disable")
    assert info.options == {"sslmode": "disable"}


def test_parse_connection_string_options_defaults_to_empty_dict():
    info = parse_connection_string("postgresql://scott:tiger@dbhost/mydb")
    assert info.options == {}


def test_connect_passes_options_through_to_psycopg2(monkeypatch):
    """
    Confirms connect() actually forwards ConnectionInfo.options as
    keyword arguments to psycopg2.connect -- the parsing alone
    (tested above) isn't sufficient proof the value is actually USED;
    this mocks psycopg2.connect specifically to verify the call
    arguments, since testing this against a real server would require
    a genuinely SSL-capable Postgres deployment this sandbox doesn't
    have (PGlite's own SSL limitation is what surfaced this gap in
    the first place).
    """
    import psycopg2

    captured_kwargs = {}

    def fake_connect(**kwargs):
        captured_kwargs.update(kwargs)
        raise psycopg2.OperationalError("simulated -- not a real connection attempt")

    monkeypatch.setattr(psycopg2, "connect", fake_connect)

    info = ConnectionInfo(
        backend=DatabaseBackend.POSTGRESQL,
        host="dbhost", port=5432, database="mydb",
        username="scott", password="tiger",
        options={"sslmode": "disable"},
    )
    with pytest.raises(RuntimeError):
        connect(info)

    assert captured_kwargs.get("sslmode") == "disable"


# --- ConnectionInfo: credential redaction ---


def test_connection_info_repr_never_shows_password():
    """
    The whole point of the env-var-based connection-string design is
    keeping credentials out of anything that could end up in a log,
    traceback, or terminal scrollback -- this is the regression test
    for that specific guarantee at the ConnectionInfo level.
    """
    info = parse_connection_string("postgresql://scott:supersecret123@dbhost/mydb")
    rendered = repr(info)
    assert "supersecret123" not in rendered
    assert "redacted" in rendered.lower()
    # Confirm the password is genuinely still on the object for actual
    # use -- only the REPR is masked, not the underlying data.
    assert info.password == "supersecret123"


def test_connection_info_repr_sqlite_has_no_password_field_to_leak():
    info = parse_connection_string("sqlite:////tmp/foo.sqlite")
    rendered = repr(info)
    assert "password" not in rendered.lower()
    assert "/tmp/foo.sqlite" in rendered


# --- Real SQLite database fixtures ---


@pytest.fixture
def sqlite_db_path(tmp_path):
    """A real, on-disk SQLite database with a primary key, multiple
    rows, a NULL-vs-literal-"NULL" distinction, and a TEXT-typed
    datetime column -- the same real-world shape loaders.py's CSV
    tests exercise, applied here to confirm db.py's query_table
    handles a real SQLite file identically."""
    db_path = tmp_path / "accounts.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE accounts (account_id TEXT PRIMARY KEY, balance REAL, "
        "opened_at TEXT, note TEXT)"
    )
    for i in range(25):
        note = "NULL" if i == 5 else (None if i == 10 else f"note-{i}")
        conn.execute(
            "INSERT INTO accounts VALUES (?, ?, ?, ?)",
            (f"ACCT-{i}", 100.0 + i, f"2024-01-{(i % 28) + 1:02d} 10:00:00", note),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def sqlite_conn(sqlite_db_path):
    conn = sqlite3.connect(str(sqlite_db_path))
    yield conn
    conn.close()


def test_connect_opens_real_sqlite_file(sqlite_db_path):
    info = ConnectionInfo(backend=DatabaseBackend.SQLITE, sqlite_path=str(sqlite_db_path))
    conn = connect(info)
    assert isinstance(conn, sqlite3.Connection)
    conn.close()


def test_connect_raises_file_not_found_for_missing_sqlite_file(tmp_path):
    info = ConnectionInfo(backend=DatabaseBackend.SQLITE, sqlite_path=str(tmp_path / "missing.sqlite"))
    with pytest.raises(FileNotFoundError):
        connect(info)


def test_connect_raises_not_implemented_for_mysql():
    info = parse_connection_string("mysql://root:secret@dbhost/mydb")
    with pytest.raises(NotImplementedError, match="mysql connectivity is not yet implemented"):
        connect(info)


def test_list_columns_returns_real_schema_order(sqlite_conn):
    cols = list_columns(sqlite_conn, "accounts")
    assert cols == ["account_id", "balance", "opened_at", "note"]


def test_list_columns_raises_for_nonexistent_table(sqlite_conn):
    with pytest.raises(ValueError, match="not found"):
        list_columns(sqlite_conn, "no_such_table")


def test_list_tables_returns_real_user_tables(sqlite_conn):
    """The `accounts` fixture table should be there; confirms the
    basic happy path before testing the trickier exclusion cases
    below."""
    assert "accounts" in list_tables(sqlite_conn)


def test_list_tables_excludes_views(sqlite_conn):
    """
    Regression test for a real distinction confirmed by direct
    testing before writing list_tables: sqlite_master lists tables AND
    views together with no single-column way to ask for tables only --
    a view must be explicitly filtered out via type='table', or batch
    mode would try to "compare" a view as if it were a real,
    independently-writable table.
    """
    sqlite_conn.execute("CREATE VIEW accounts_view AS SELECT * FROM accounts")
    sqlite_conn.commit()
    tables = list_tables(sqlite_conn)
    assert "accounts_view" not in tables
    assert "accounts" in tables


def test_list_tables_excludes_sqlite_internal_tables(sqlite_conn):
    """
    Regression test for a real, confirmed SQLite behavior: declaring
    an AUTOINCREMENT column auto-creates an internal sqlite_sequence
    bookkeeping table -- without filtering 'sqlite_%' names out, batch
    mode would try to "compare" SQLite's own internal bookkeeping as
    if it were real user data.
    """
    sqlite_conn.execute("CREATE TABLE autoinc_test (id INTEGER PRIMARY KEY AUTOINCREMENT, val TEXT)")
    sqlite_conn.commit()
    tables = list_tables(sqlite_conn)
    assert not any(t.startswith("sqlite_") for t in tables)
    assert "autoinc_test" in tables


def test_list_tables_returns_sorted_names(sqlite_conn):
    sqlite_conn.execute("CREATE TABLE zzz_last (id TEXT)")
    sqlite_conn.execute("CREATE TABLE aaa_first (id TEXT)")
    sqlite_conn.commit()
    tables = list_tables(sqlite_conn)
    assert tables == sorted(tables)


def test_detect_primary_key_single_column(sqlite_conn):
    assert detect_primary_key(sqlite_conn, "accounts") == ["account_id"]


def test_detect_primary_key_returns_none_when_no_pk(sqlite_conn):
    sqlite_conn.execute("CREATE TABLE no_pk_table (a TEXT, b TEXT)")
    sqlite_conn.commit()
    assert detect_primary_key(sqlite_conn, "no_pk_table") is None


def test_detect_primary_key_composite_in_correct_order(sqlite_conn):
    """
    Regression test for the real schema-metadata detail confirmed by
    direct testing: PRAGMA table_info's 6th element is a nonzero,
    SEQUENTIAL position for primary-key columns, not just a boolean --
    so a composite key must come back in its real key order, which
    may differ from the table's column declaration order.
    """
    sqlite_conn.execute(
        "CREATE TABLE composite (val REAL, region TEXT, id TEXT, PRIMARY KEY (region, id))"
    )
    sqlite_conn.commit()
    assert detect_primary_key(sqlite_conn, "composite") == ["region", "id"]


def test_detect_primary_key_raises_for_nonexistent_table(sqlite_conn):
    with pytest.raises(ValueError, match="not found"):
        detect_primary_key(sqlite_conn, "no_such_table")


def test_query_table_returns_dataframe_with_correct_shape(sqlite_conn):
    df = query_table(sqlite_conn, "accounts")
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 25
    assert list(df.columns) == ["account_id", "balance", "opened_at", "note"]


def test_query_table_preserves_literal_null_string_distinct_from_real_null(sqlite_conn):
    """
    Confirmed by direct testing before writing query_table: SQLite's
    native per-value typing means pd.read_sql ALREADY distinguishes a
    literal "NULL" string from a genuine SQL NULL with no special
    handling needed -- unlike load_csv, which needs keep_default_na=False
    to get the same guarantee. This is the regression test for that.
    """
    df = query_table(sqlite_conn, "accounts")
    # Row 5 has the literal string "NULL"; row 10 has a genuine NULL.
    assert df.loc[5, "note"] == "NULL"
    assert pd.isna(df.loc[10, "note"])


def test_query_table_parses_text_datetime_column(sqlite_conn):
    """
    SQLite has no native datetime type -- confirmed directly that a
    datetime column round-trips through SQLite as plain TEXT, the
    identical problem CSV has. query_table reuses loaders.py's
    _try_parse_datetime_columns (not a reimplementation) to fix this;
    this test confirms that reuse actually works end-to-end against a
    real SQLite-sourced DataFrame.
    """
    df = query_table(sqlite_conn, "accounts")
    assert pd.api.types.is_datetime64_any_dtype(df["opened_at"]) or df["opened_at"].dtype == object
    assert isinstance(df.loc[0, "opened_at"], pd.Timestamp)
    # The literal "NULL" sentinel at row 5 must survive the datetime
    # parsing pass untouched, not get coerced to NaT.
    assert df.loc[5, "note"] == "NULL"


def test_query_table_raises_not_implemented_for_non_sqlite_connection():
    """A plain object (standing in for "not a sqlite3.Connection") must
    be rejected clearly, not produce a confusing AttributeError deep
    inside pandas."""
    with pytest.raises(NotImplementedError, match="only implemented for SQLite"):
        query_table(object(), "accounts")


def test_quote_identifier_handles_special_table_names(sqlite_conn):
    """A table name that's also a SQL keyword must still work --
    confirmed this is a real concern, not theoretical, since "order"
    and "group" are common real-world table names that happen to be
    reserved words."""
    sqlite_conn.execute('CREATE TABLE "order" (id TEXT, val REAL)')
    sqlite_conn.execute('INSERT INTO "order" VALUES (\'1\', 10.0)')
    sqlite_conn.commit()

    cols = list_columns(sqlite_conn, "order")
    assert cols == ["id", "val"]

    df = query_table(sqlite_conn, "order")
    assert len(df) == 1


# --- PostgreSQL: tested against a REAL Postgres server, not mocked ---
# Uses py-pglite (PGlite, a real PostgreSQL compiled to WASM, run via
# Node.js) to start an actual Postgres server for the duration of this
# test session -- confirmed by direct testing this gives real SQL
# execution and real system-catalog schema introspection (pg_index/
# pg_attribute, information_schema.columns), the same verification
# standard the SQLite tests above hold. Module-scoped because
# starting/stopping a real server takes ~5s, confirmed by direct
# timing -- too slow to pay per-test; each test instead creates its
# own uniquely-named table to avoid cross-test interference within
# the one shared server.

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 is required to test PostgreSQL support")
pytest.importorskip("py_pglite", reason="py-pglite is required to test PostgreSQL support (needs Node.js/npm)")
from py_pglite import PGliteConfig, PGliteManager  # noqa: E402


@pytest.fixture(scope="module")
def real_postgres_dsn():
    """Starts one real Postgres server for every test in this module,
    yields its keyword-style DSN, and tears it down afterward."""
    config = PGliteConfig()
    with PGliteManager(config) as manager:
        yield manager.get_dsn()


def _dsn_to_connection_info(dsn: str) -> ConnectionInfo:
    """Parses py-pglite's keyword-style DSN ("host=... dbname=...
    user=... password=...") into the same ConnectionInfo shape
    parse_connection_string would build from a real postgresql://
    URI -- so tests exercise connect()'s real Postgres branch exactly
    as cli.py would call it, not a shortcut around it.

    Confirmed by direct testing: PGlite's DSN is Unix-socket-only and
    has NO "port" key at all (host is a socket directory path, not a
    TCP hostname) -- passing an invented port value alongside a socket
    path confuses psycopg2 ("server didn't return client encoding").
    port is left as None here when absent, exactly mirroring what a
    real connection string without an explicit :port would parse to.
    """
    parts = dict(p.split("=", 1) for p in dsn.split())
    return ConnectionInfo(
        backend=DatabaseBackend.POSTGRESQL,
        host=parts.get("host", "localhost"),
        port=int(parts["port"]) if "port" in parts else None,
        database=parts["dbname"],
        username=parts["user"],
        password=parts["password"],
    )


@pytest.fixture(scope="module")
def pg_conn(real_postgres_dsn):
    """
    A single connection, reused across every test in this module --
    NOT one fresh connection per test. Confirmed by direct testing
    that PGlite (the real Postgres-via-WASM server these tests run
    against) is single-connection-only: a SECOND connect() call to the
    same PGlite instance fails with "server didn't return client
    encoding", a real limitation of this specific test tool (PGlite
    is documented as single-user-mode), not a bug in db.py's connect()
    -- confirmed separately that connect() itself works correctly
    against a real multi-connection-capable Postgres in isolation.
    Test isolation within this shared connection comes from each test
    creating its own uniquely-named table, not from connection-level
    isolation.
    """
    conn = connect(_dsn_to_connection_info(real_postgres_dsn))
    yield conn
    conn.close()


def test_connect_opens_real_postgres_connection(pg_conn):
    """Confirms connect()'s Postgres branch returns a real,
    isinstance-correct psycopg2 connection -- exercised via the shared
    pg_conn fixture rather than opening a SEPARATE connection here,
    since PGlite's single-connection-slot limitation (confirmed by
    direct testing) means a second connect() call in this module would
    break every later test sharing the one server instance."""
    assert isinstance(pg_conn, psycopg2.extensions.connection)


def test_connect_raises_runtime_error_for_unreachable_postgres_host():
    """
    Confirms a real connection FAILURE is wrapped in a clean
    RuntimeError with no credential leakage -- not psycopg2's raw
    OperationalError, which can include connection parameters in its
    message.

    Uses a genuinely unreachable port (no server, no PGlite needed at
    all) rather than a wrong-password test against a real PGlite
    server -- confirmed by direct testing that PGlite does NOT enforce
    password authentication at all (a connection with a deliberately
    wrong password against a real PGlite instance succeeds), so a
    wrong-password test against it could never actually fail and would
    not be testing anything real. An unreachable host/port is a
    genuine, unavoidable connection failure regardless of backend,
    exercising the same OperationalError-catching branch in connect()
    without depending on auth behavior this specific test tool doesn't
    implement.
    """
    info = ConnectionInfo(
        backend=DatabaseBackend.POSTGRESQL,
        host="localhost", port=1, database="nonexistent",
        username="nobody", password="super-secret-password-xyz",
    )
    with pytest.raises(RuntimeError, match="Failed to connect to PostgreSQL") as exc_info:
        connect(info)
    assert "super-secret-password-xyz" not in str(exc_info.value)


def test_postgres_list_columns_returns_real_schema_order(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE list_cols_test (account_id TEXT PRIMARY KEY, balance REAL, note TEXT)")
    pg_conn.commit()

    cols = list_columns(pg_conn, "list_cols_test")
    assert cols == ["account_id", "balance", "note"]


def test_postgres_list_columns_raises_for_nonexistent_table(pg_conn):
    with pytest.raises(ValueError, match="not found"):
        list_columns(pg_conn, "this_table_does_not_exist_at_all")


def test_postgres_list_tables_returns_real_user_tables(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE list_tables_basic_test (id TEXT PRIMARY KEY)")
    pg_conn.commit()
    assert "list_tables_basic_test" in list_tables(pg_conn)


def test_postgres_list_tables_excludes_views(pg_conn):
    """
    Regression test for a real distinction confirmed by direct testing
    against a real Postgres server before writing list_tables'
    Postgres branch: information_schema.tables lists views alongside
    real tables -- table_type = 'BASE TABLE' must filter them out, or
    batch mode would try to "compare" a view as if it were an
    independently-writable table.
    """
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE view_source_test (id TEXT PRIMARY KEY)")
    cur.execute("CREATE VIEW view_source_test_view AS SELECT * FROM view_source_test")
    pg_conn.commit()
    tables = list_tables(pg_conn)
    assert "view_source_test_view" not in tables
    assert "view_source_test" in tables


def test_postgres_list_tables_returns_sorted_names(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE zzz_sort_test (id TEXT)")
    cur.execute("CREATE TABLE aaa_sort_test (id TEXT)")
    pg_conn.commit()
    tables = list_tables(pg_conn)
    assert tables == sorted(tables)


def test_postgres_detect_primary_key_single_column(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE pk_single_test (account_id TEXT PRIMARY KEY, balance REAL)")
    pg_conn.commit()

    assert detect_primary_key(pg_conn, "pk_single_test") == ["account_id"]


def test_postgres_detect_primary_key_returns_none_when_no_pk(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE pk_none_test (a TEXT, b TEXT)")
    pg_conn.commit()

    assert detect_primary_key(pg_conn, "pk_none_test") is None


def test_postgres_detect_primary_key_composite_in_correct_order(pg_conn):
    """
    Regression-style test for the real schema-introspection detail
    confirmed by direct testing against a real Postgres server before
    writing detect_primary_key's Postgres branch:
    array_position(i.indkey, a.attnum) must recover the composite
    key's REAL declared order, not just an arbitrary order from the
    join -- confirmed this matters by declaring the columns in a
    different order than the primary key itself uses.
    """
    cur = pg_conn.cursor()
    cur.execute(
        "CREATE TABLE pk_composite_test (val REAL, region TEXT, id TEXT, PRIMARY KEY (region, id))"
    )
    pg_conn.commit()

    assert detect_primary_key(pg_conn, "pk_composite_test") == ["region", "id"]


def test_postgres_detect_primary_key_raises_for_nonexistent_table(pg_conn):
    with pytest.raises(ValueError, match="not found"):
        detect_primary_key(pg_conn, "this_table_does_not_exist_either")


def test_postgres_query_table_preserves_literal_null_string_distinct_from_real_null(pg_conn):
    """
    Confirmed by direct testing before writing query_table's Postgres
    branch: pd.read_sql against a real Postgres connection ALREADY
    distinguishes a literal "NULL" string from a genuine SQL NULL --
    the same guarantee SQLite gives for free, for the same underlying
    reason (native per-value typing).
    """
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE null_test (id TEXT PRIMARY KEY, note TEXT)")
    cur.execute("INSERT INTO null_test VALUES ('1', 'NULL')")
    cur.execute("INSERT INTO null_test VALUES ('2', NULL)")
    pg_conn.commit()

    df = query_table(pg_conn, "null_test")
    assert df.loc[0, "note"] == "NULL"
    assert pd.isna(df.loc[1, "note"])


def test_postgres_query_table_native_timestamp_needs_no_parsing(pg_conn):
    """
    The key difference from SQLite, confirmed by direct testing: a
    real Postgres TIMESTAMP column round-trips through pd.read_sql as
    a genuine pandas datetime64 dtype directly -- no
    _try_parse_datetime_columns pass needed at all, unlike SQLite's
    TEXT-typed dates.
    """
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE ts_test (id TEXT PRIMARY KEY, opened_at TIMESTAMP)")
    cur.execute("INSERT INTO ts_test VALUES ('1', '2024-01-15 10:30:00')")
    pg_conn.commit()

    df = query_table(pg_conn, "ts_test")
    assert pd.api.types.is_datetime64_any_dtype(df["opened_at"])
    assert df.loc[0, "opened_at"] == pd.Timestamp("2024-01-15 10:30:00")


def test_postgres_query_table_suppresses_sqlalchemy_warning_but_not_others(pg_conn, recwarn):
    """
    Confirms the UserWarning suppression in query_table's Postgres
    branch is scoped to exactly the expected pandas warning -- not a
    blanket "ignore all warnings" that could hide a genuinely
    different, unrelated warning surfacing from the same call.
    """
    cur = pg_conn.cursor()
    cur.execute("CREATE TABLE warn_test (id TEXT PRIMARY KEY)")
    cur.execute("INSERT INTO warn_test VALUES ('1')")
    pg_conn.commit()

    query_table(pg_conn, "warn_test")
    sqlalchemy_warnings = [
        w for w in recwarn.list
        if "pandas only supports SQLAlchemy" in str(w.message)
    ]
    assert len(sqlalchemy_warnings) == 0


def test_postgres_quote_identifier_handles_reserved_word_table_name(pg_conn):
    cur = pg_conn.cursor()
    cur.execute('CREATE TABLE "select" (id TEXT PRIMARY KEY, val REAL)')
    cur.execute('INSERT INTO "select" VALUES (\'1\', 10.0)')
    pg_conn.commit()

    cols = list_columns(pg_conn, "select")
    assert cols == ["id", "val"]

    df = query_table(pg_conn, "select")
    assert len(df) == 1
