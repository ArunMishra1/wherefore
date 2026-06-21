# Troubleshooting

Specific, real failure modes — what they look like, why they happen,
and how to fix them. If something isn't here, check the error message
itself first: `wherefore` is written to fail loudly with a specific
reason rather than a generic stack trace, so the message usually says
exactly what's wrong.

### Contents

[Installation](#installation) · [Reading files](#reading-files) ·
[Key detection and matching](#key-detection-and-matching) ·
[Database sources](#database-sources) · [S3](#s3) ·
[`--explain` / the AI layer](#--explain--the-ai-layer) ·
[Eval harness](#eval-harness) · [Still stuck?](#still-stuck)

---

## Installation

### `pip install wherefore` fails on a very new Python version

`wherefore` is tested on Python 3.10–3.13. On 3.14+, the core
dependencies (pandas, numpy) are usually fine, but a smaller
transitive dependency somewhere in the chain may not have published
wheels for that version yet. If `pip install` fails with a build error
mentioning a package you've never heard of, that's almost always the
cause — try 3.11/3.12/3.13 instead, or wait a few weeks for the
ecosystem to catch up.

### `wherefore[db]` fails to install psycopg2

`wherefore[db]` installs `psycopg2-binary`, the precompiled variant —
this should "just work" via `pip` with no system libraries required.
If you've pinned a different `psycopg2` package yourself (a lockfile
specifying plain `psycopg2`, the source variant, for example): plain
`psycopg2` needs `libpq-dev`/`pg_config` and a C compiler already
present on your system — confirmed directly that it fails outright
without them. Either install those system dependencies, or switch back
to `psycopg2-binary`.

### Homebrew install builds from source and takes several minutes

This is expected on a platform/macOS version that doesn't match the
tap's prebuilt bottle (currently Apple Silicon + a specific macOS
version — see the tap's own README for exactly which). The from-source
build genuinely needs to compile pandas/numpy's C extensions and, for
some transitive dependencies, a Rust toolchain — this is a one-time
cost per machine, not a bug. If it fails partway through, check
`brew install --build-from-source tracelore/wherefore/wherefore -v`
for the actual failure — five distinct real build issues were found
and fixed while setting up the tap (documented in the tap's own
`README.md`), so if you hit something new, it's worth reporting.

---

## Reading files

### `UnicodeDecodeError` when comparing a CSV

This is **intentional**, not a bug. `wherefore` defaults to strict
UTF-8 and does not silently guess or fall back to another encoding —
because the decode failure itself is often the actual signal
`encoding_mismatch` exists to detect (e.g. a Latin-1 file misread as
UTF-8 downstream).

If you genuinely have a non-UTF-8 source file: there is currently no
`--encoding` flag on the CLI itself (confirmed by checking — this is a
real, honest gap, not hidden) — `load_csv()`'s underlying `encoding`
parameter exists in the Python API but isn't yet exposed as a CLI
option. The workaround today is converting the file to UTF-8 first
(e.g. `iconv -f latin1 -t utf-8 old.csv > old_utf8.csv`), or calling
`wherefore`'s Python functions directly instead of the CLI if you need
this now. Worth raising as a feature request if you hit this.

### A datetime column shows up as `pattern unrecognized` even though the dates clearly shifted

Two real causes, both worth checking:

1. **It round-tripped through a format with no native datetime type.**
   CSV doesn't have one — `wherefore` does its best to detect and
   parse datetime-looking columns automatically, but a column that's
   less than ~80% parseable dates (e.g. mixed with free text, or a
   lot of literal sentinel values like `"N/A"`) is deliberately left
   as plain text rather than risk corrupting it. Parquet and Excel
   don't have this problem — their native typing survives the
   round-trip with no parsing step needed.
2. **A literal sentinel value blocked detection entirely on an older
   version.** This was a real bug found and fixed early on — a single
   `"NULL"` string among genuine dates used to block the *whole
   column* from being recognized as datetime. If you're on a version
   from before that fix, upgrading resolves it.

### A column that should be a date is full of `NaT`/garbage values

If you're comparing a CSV where one side genuinely has a literal
`"NULL"`/`"N/A"` string sitting in a date column, that's working as
intended — `wherefore` deliberately preserves that literal string
rather than guessing it should be null, since that distinction is
often itself the bug you're trying to find (a real null on one side,
a string sentinel on the other).

---

## Key detection and matching

### `Could not auto-detect a join key column. Pass one explicitly with --key.`

`wherefore` looks for a column present on both sides that's at least
95% unique — if nothing qualifies (e.g. every column has duplicates,
or the two files genuinely share no column names), it asks you to be
explicit rather than guess wrong:

```bash
wherefore compare old.csv new.csv --key employee_id
```

### Two files that should match show up entirely as "rows only in
source" / "rows only in target"

The most common real cause: the join key is formatted differently on
each side (`EMP-1001` vs `EMP1001`, common after a migration that
normalized ID formatting). Add `--fuzzy-keys`:

```bash
wherefore compare old.csv new.csv --key employee_id --fuzzy-keys
```

If that still doesn't resolve some rows, `wherefore` will tell you
which keys were ambiguous (multiple equally-plausible matches) rather
than guess — those need a closer look, since forcing a guess on an
ambiguous match risks comparing two genuinely different records.

### `--fuzzy-keys` resolved most rows but a handful are still unmatched

This is likely the `key_mismatch` taxonomy pattern itself — check the
generated report. If the same literal transformation (e.g. "every
mismatch is explained by stripping dashes") explains all of them,
that's a real, systematic formatting difference worth fixing upstream,
not a `wherefore` bug.

---

## Database sources

### `Error connecting to database: ... uses the db:// syntax but no connection-string environment variable was given`

You used `db://table_name` but forgot `--source-conn-env`/
`--target-conn-env`. These pass the *name* of an environment variable,
not a connection string directly:

```bash
export SOURCE_DB="sqlite:////absolute/path/to/file.sqlite"
wherefore compare db://accounts other.csv --source-conn-env SOURCE_DB --key id
```

### `Environment variable 'X' is not set`

You passed `--source-conn-env SOURCE_DB` but never actually
`export`ed `SOURCE_DB` in the same shell session. Environment
variables don't persist across terminal sessions or `sudo` unless
explicitly exported — set it again before running the command.

### `Malformed sqlite connection string` mentioning slash counts

SQLite connection strings follow a specific, easy-to-get-wrong
convention (the same one SQLAlchemy uses):

- **Relative path** — 3 slashes: `sqlite:///relative/path.sqlite`
- **Absolute path** — 4 slashes: `sqlite:////absolute/path.sqlite`
  (the 4th slash is the path's own leading slash, not a separator —
  this is the single most common mistake people make with this
  format)

### Postgres connection fails with an SSL-related error

Some Postgres setups (managed/cloud databases, some local dev
environments, some lightweight testing servers) need explicit SSL
behavior. Pass it as a query parameter on the connection string:

```bash
export SOURCE_DB="postgresql://user:password@host:5432/mydb?sslmode=require"
# or, for a server that explicitly can't negotiate SSL:
export SOURCE_DB="postgresql://user:password@host:5432/mydb?sslmode=disable"
```

### A composite (multi-column) primary key was detected but the comparison fails

`wherefore` detects and shows composite primary keys from the
database's own schema, but doesn't yet support using one directly as
the join key. Pass `--key` with a single column name to proceed with
just that column, or treat this as a signal that the table's real
identity needs more modeling than a one-column comparison can give
you.

### The primary-key confirmation prompt is annoying in a script

Pass `--yes` (or `-y`) once you trust the auto-detection:

```bash
wherefore compare db://accounts db://accounts \
    --source-conn-env SOURCE_DB --target-conn-env TARGET_DB --yes
```

This is the one safety check standing between a wrong guess and a
query running against a real database unreviewed — skip it
deliberately, not by accident.

---

## S3

### `Failed to read s3://bucket/key.csv from S3`

Almost always one of: the bucket name is misspelled, the key
(filepath within the bucket) is wrong, or your AWS credentials don't
have read access to that specific bucket/key. Double-check with
`aws s3 ls s3://bucket/key.csv` (using the AWS CLI directly) to
isolate whether it's a `wherefore` problem or a credentials/permissions
problem.

### `No AWS credentials found`

`wherefore` uses the standard AWS credential chain (environment
variables, `~/.aws/credentials`, an IAM role, or `AWS_PROFILE`) — it
doesn't invent its own. If none of those are set up, no S3 access will
work regardless of what you pass to `wherefore`. Run
`aws sts get-caller-identity` to confirm your credentials are even
working at all, independent of `wherefore`.

### `pip install wherefore[s3]` step was skipped

`boto3` is optional — if you haven't installed the `s3` extra, any
`s3://` path will fail with a clear `ImportError` telling you to run
`pip install wherefore[s3]`. This is intentional (most users never
touch S3, so nobody pays for `boto3`'s install weight unless they
actually use it).

---

## `--explain` / the AI layer

### `--explain requires ANTHROPIC_API_KEY to be set in your environment`

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
wherefore compare old.csv new.csv --explain
```

This is checked *before* any diffing/clustering work runs, so a
missing key fails immediately rather than partway through.

### `--explain` is making real, billed API calls and I didn't expect that

This is by design, clearly flagged in the flag's own help text — the
statistical pipeline (everything `wherefore` does *without*
`--explain`) is completely free and makes zero network calls. Only
`--explain` talks to the Claude API, and only when you explicitly pass
it.

### I'm worried about sensitive data reaching the API

`--explain` redacts emails, SSNs, credit card numbers, and US phone
numbers *before* anything is sent, on by default — no flag needed.
Anything redacted is reported back to you in the output
(`Redacted before sending to Claude: email`). This is **pattern-based
detection of structurally recognizable data, not a general PII
scanner** — it won't catch a name or a home address. See
[`SECURITY.md`](./SECURITY.md) for the full scope, including a
documented false-positive case (long numeric IDs can resemble card
numbers).

---

## Eval harness

### `python3 -m evals.harness.run_eval` shows fewer than 7 cases, or different numbers than the README

The statistical eval harness currently only scores the 6
column-mismatch patterns plus one honest-abstain fixture (7 total) —
`dedup_failure` and `key_mismatch` are row-presence patterns with their
own dedicated unit tests, but aren't wired into this specific harness
yet. This is a known, tracked gap (see `TAXONOMY_TODO.md`), not a bug
if your numbers match that shape.

### Running `--llm` mode doesn't do anything / asks for an API key

`python3 -m evals.harness.run_eval --llm` makes real, billed Claude API
calls — it requires `ANTHROPIC_API_KEY` to be set, the same as
`--explain` does. The default (no `--llm`) statistical-only mode is
free and makes zero network calls.

---

## Still stuck?

Check the actual error message text against the source first —
`wherefore`'s error messages are written to be specific and actionable
rather than generic, so if something doesn't match what's documented
here, the message itself is probably the more accurate source. If you
think you've found a real bug, see
[`CONTRIBUTING.md`](./CONTRIBUTING.md) for how to report it, or open
an issue with the exact command you ran and the full error output.
