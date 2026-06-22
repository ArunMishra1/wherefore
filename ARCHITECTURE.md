# Architecture

An orientation doc — where things live, how the pieces connect, and
what's genuinely true about the codebase versus what looks plausible
but isn't. Written for anyone (human or AI) trying to get oriented
quickly, without re-deriving the whole thing by reading every file.

If you're adding a new taxonomy pattern specifically, see
[`CONTRIBUTING.md`](./CONTRIBUTING.md) instead — that's the step-by-step
for the most common contribution. This doc is the wider map.

### Contents

[The pipeline, end to end](#the-pipeline-end-to-end) ·
[Module map](#module-map) ·
[Things that look like one thing but are another](#things-that-look-like-one-thing-but-are-another) ·
[Key design decisions and why](#key-design-decisions-and-why) ·
[Where the tests are](#where-the-tests-are) ·
[If you're starting a new session on this codebase](#if-youre-starting-a-new-session-on-this-codebase)

---

## The pipeline, end to end

```
source, target   (file path, s3:// URL, or db://table_name)
      │
      ▼
load the data     comparison/loaders.py (files) or comparison/db.py (databases)
      │            → always returns a plain pandas DataFrame; nothing
      │              downstream of this point knows or cares where the
      │              data came from
      ▼
resolve the join key   cli.py: explicit --key, or auto-detected (files:
      │                 a uniqueness heuristic; databases: real schema
      │                 introspection, shown to the user for confirmation
      │                 before anything runs — these are NOT the same
      │                 mechanism, deliberately, see below)
      ▼
diff            comparison/diff_engine.py (wraps datacompy)
      │          → produces a DiffResult: column-level mismatches,
      │            plus source-only/target-only rows for anything that
      │            didn't match at all
      ▼
cluster         clustering/cluster_mismatches.py + clustering/signatures.py
      │          → groups mismatches by column, runs cheap statistical
      │            checks against the taxonomy, and reports candidate
      │            pattern matches WITH A CONFIDENCE SCORE — never a
      │            causal claim. "These 12 rows differ by exactly 5
      │            hours" is as far as this layer ever goes.
      │
      ├─── default: stop here. Free, no API key, no network call.
      │
      ▼  only with --explain
explain         reasoning/explain.py + reasoning/providers/
      │          → takes a cluster's statistical facts, asks Claude to
      │            produce the plain-English causal narrative, citing
      │            real example rows. reasoning/redaction.py runs first,
      │            unconditionally, scrubbing anything that looks like
      │            an email/SSN/card number/phone number.
      ▼
render the report      cli.py's _render_report (NOT reasoning/report.py —
                        see "things that look like one thing" below)
```

## Module map

```
src/wherefore/
├── cli.py                    The actual `wherefore compare` / `compare-dir`
│                             commands. This is where flags, key
│                             auto-detection, the database-confirmation
│                             prompt, and report rendering all live —
│                             it's a bigger file than you'd expect for
│                             a reason: it's the orchestration layer,
│                             not just argument parsing.
│
├── comparison/
│   ├── loaders.py            File-format dispatch (CSV/JSON/Parquet/
│   │                         Excel, local or s3://). Look here first
│   │                         for anything about reading a file wrong
│   │                         (null handling, datetime parsing, encoding).
│   ├── db.py                 Database connectivity (SQLite, PostgreSQL —
│   │                         see below for MySQL's status). Mirrors
│   │                         loaders.py's "check the special case before
│   │                         doing anything path-like" discipline one
│   │                         level up, for db:// CLI sources specifically.
│   ├── key_matching.py       Fuzzy key resolution (--fuzzy-keys) — rapidfuzz-
│   │                         based, with confidence scores kept, not discarded.
│   ├── diff_engine.py        Wraps datacompy. The DiffResult dataclass
│   │                         (in diff_result.py) is the contract every
│   │                         layer above this one consumes.
│   └── diff_result.py        The DiffResult/MismatchRow/RowPresenceRecord
│                             dataclasses — read these before touching
│                             clustering, since they're the actual interface.
│
├── clustering/
│   ├── cluster_mismatches.py Groups mismatches by column into Cluster
│   │                         objects, dispatches to taxonomy patterns by
│   │                         dtype, and (separately) detect_row_presence_
│   │                         patterns() for findings that show up as
│   │                         entirely missing/extra rows instead of
│   │                         column mismatches (dedup_failure, key_mismatch).
│   └── signatures.py         The actual statistical signature functions
│                             (one per taxonomy pattern, roughly). This is
│                             where "is this a timezone shift" gets checked,
│                             as a number between 0 and 1 — never a sentence.
│
├── taxonomy/
│   ├── schema.py             The PatternDefinition pydantic schema every
│   │                         pattern YAML must validate against.
│   ├── registry.py           Loads + validates every patterns/*.yaml at
│   │                         startup; dispatches by dtype.
│   └── patterns/*.yaml        One YAML file per failure pattern — the
│                             taxonomy IS these files, not code.
│
├── synthetic/
│   ├── base_dataset.py        Generates clean, realistic fixture data
│   │                         (the FINANCIAL_ACCOUNTS schema, etc.)
│   ├── corruptors/*.py        One function per pattern: takes clean data,
│   │                         deliberately breaks it one specific way,
│   │                         returns the broken data PLUS what it did —
│   │                         this is the ground truth the eval harness
│   │                         scores against.
│   └── ground_truth.py        The labeled-fixture bookkeeping.
│
├── reasoning/
│   ├── explain.py             Turns a Cluster's statistics into a prompt,
│   │                         calls a Provider, parses the structured
│   │                         response into a ClusterExplanation.
│   ├── redaction.py            Pattern-based PII scrubbing, runs before
│   │                         ANY data reaches explain.py's prompt.
│   ├── providers/             The Provider ABC (one method: complete())
│   │                         plus the real ClaudeProvider implementation.
│   │                         Tests use a fake Provider — no real API
│   │                         calls in the normal test suite.
│   └── report.py              DEAD CODE — see below.
│
└── evals/
    ├── harness/run_eval.py     The actual eval entry point
    │                         (`python3 -m evals.harness.run_eval`).
    ├── harness/scoring.py     Precision/recall scoring against ground truth.
    └── fixtures/               Committed labeled fixtures — generated
                                once, reviewed, committed; NOT regenerated
                                automatically on every run (see CONTRIBUTING.md
                                for why).
```

## Things that look like one thing but are another

These are real, confirmed gaps between what the codebase *suggests* and
what's *actually true* — worth knowing before you assume otherwise and
waste time.

- **`reasoning/report.py` is dead code.** It's a stub with a
  `"NEXT TURN: implement this"` comment and nothing else. The real
  report-rendering logic lives in `cli.py`'s `_render_report` function.
  Don't assume report formatting changes belong in `reasoning/report.py`
  — they don't, and that file has never been wired into anything.
- **File auto-detection and database auto-detection are NOT the same
  mechanism, on purpose.** `cli.py`'s `_auto_detect_key` (files) is a
  uniqueness-ratio heuristic with no confirmation step — it just picks
  a column and proceeds silently. `_detect_db_primary_keys` (databases)
  reads the real schema and *requires* interactive confirmation before
  anything runs. This isn't inconsistency to "fix" — a wrong guess
  against a real, possibly-production database is a materially worse
  mistake than a wrong guess against a CSV, so the two paths are
  deliberately asymmetric.
- **`dedup_failure` and `key_mismatch` don't show up as column
  mismatches at all.** They're row-presence patterns — their signal is
  an entire row missing/extra, not a value differing in a column. They
  go through `detect_row_presence_patterns()`, a separate function from
  `cluster_mismatches()`, specifically so the widely-used
  `cluster_mismatches() -> list[Cluster]` contract never had to change
  shape for them.
- **Neither `dedup_failure` nor `key_mismatch` is wired into the eval
  harness yet.** `evals/harness/run_eval.py` only scores column-mismatch
  `Cluster` results today. Both patterns have real, dedicated unit
  tests instead — this is a known, tracked gap (see `TAXONOMY_TODO.md`),
  not something accidentally skipped.
- **PostgreSQL support is real, tested against an actual running
  Postgres server — not mocked.** (Via `py-pglite`/PGlite, a real
  Postgres compiled to WASM.) MySQL, despite sharing the same
  connection-string parsing code path, is NOT implemented at all —
  `connect()`/`query_table()`/etc. all raise a clear `NotImplementedError`
  for it. The connection-string *format* is generic across all three
  backends; the actual connectivity code is not.
- **`compare-dir` now supports `db://*` batch mode (database vs
  database), but NOT mixed sources (one side a directory, the other a
  database).** The real design pass this previously needed (see the
  build log for the specifics) concluded: two databases can be paired
  by table name the same way two directories are paired by filename
  (`list_tables` + intersection, mirroring `_match_files_by_name`
  exactly), so that case was built. Mixing a directory with a database
  was deliberately left unsolved and rejected with a clear error
  rather than guessed at — pairing a table name against a filename
  isn't symmetric the way two directories or two databases are.

## Key design decisions and why

**Clustering never makes causal claims — only the AI layer does, and
only when asked.** `clustering/` produces statistical observations
("these 12 rows differ by exactly 5 hours"), never causal language
("this is a timezone bug"). If this boundary gets blurred, the AI
layer becomes decorative and the eval harness stops measuring anything
real — it would just be checking whether the LLM repeats what
clustering already concluded.

**Failure patterns are data (YAML), not code.** Adding a pattern means
writing a YAML file plus a small corruptor function — never touching
clustering, reasoning, or registry code. This is mechanically enforced
by `taxonomy/registry.py`'s dispatch-by-dtype design, not just a
convention people are trusted to follow.

**Everything that touches an external system (S3, a database, the
Claude API) treats its dependency as optional.** `boto3` (S3) and
`psycopg2` (Postgres) are both `pip install wherefore[extra]` add-ons,
not core dependencies — most users never touch either, so nobody pays
their install weight by default. The pattern is always the same:
import lazily inside the function that needs it, catch `ImportError`,
re-raise with a clear "install with `pip install wherefore[x]`"
message instead of letting a raw `ModuleNotFoundError` surface.

**Credentials never appear in argv or shell history.** `--source-conn-env`/
`--target-conn-env` pass the *name* of an environment variable, never a
connection string directly. `ConnectionInfo` (in `comparison/db.py`)
overrides `__repr__` specifically to keep a password out of any
traceback or log line, even though the field exists on the object for
real use.

**Redaction runs before data reaches an API call, unconditionally.**
`reasoning/redaction.py` is checked on every `--explain` call by
default — `--no-redact` is opt-out, not opt-in. This was built and
landed *before* any cloud-storage or database connectivity work
started, specifically because each new source type widens the gap
between "data on the user's machine" and "data that could reach an
external API."

## Where the tests are

Tests mirror `src/wherefore/`'s structure 1:1 under `tests/`. A few
things worth knowing before adding more:

- **Real verification over mocking, wherever practical.** S3 tests use
  `moto` (a real mocked AWS backend, not a stubbed client). SQLite
  tests use real on-disk `.sqlite` files via stdlib `sqlite3`.
  PostgreSQL tests use a real running Postgres server via `py-pglite`
  (confirmed: this genuinely runs real SQL against a real server, not
  a fake one) — module-scoped because starting/stopping it costs ~5s,
  with tests sharing one connection (PGlite is single-connection-only,
  confirmed by hitting that limit directly while building these tests).
- **CLI tests use `typer.testing.CliRunner` against the real `compare`
  command**, not by calling internal functions directly — this is what
  caught real bugs before (Typer collapsing a single subcommand,
  a misplaced success-message print statement that crashed the CLI
  in one specific code path).
- **The eval harness is a separate thing from `pytest`.** It's its own
  entry point (`python3 -m evals.harness.run_eval`), not discoverable
  by `pytest` at all — there genuinely are no `test_*.py` files under
  `evals/harness/`. Don't expect `pytest evals/` to do anything
  meaningful; this was a real, confirmed CI bug at one point (a CI step
  that looked like it was testing the eval harness but was silently
  collecting zero tests).

## If you're starting a new session on this codebase

Read the actual code before assuming anything about it — this project's
own working convention (see `TAXONOMY_TODO.md`'s entire existence) is
that every claim gets verified against real output before it's written
down, including claims that sound obviously true. Several real bugs in
this codebase's history came from a plausible-sounding assumption that
turned out to be subtly wrong once actually tested (a magnitude
heuristic that looked right but had a real false positive; a
`urlparse`-based SQLite path parser that didn't match the documented
convention; a fuzzy-similarity threshold for `key_mismatch` that scored
unrelated keys just as confidently as genuine reformats). The fix in
every case was the same: stop reasoning from documentation or
intuition, and run the actual code against a real example first.
