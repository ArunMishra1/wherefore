# wherefore

**Tells you *why* two datasets differ — not just that they do.**

[![CI](https://github.com/tracelore/wherefore/actions/workflows/ci.yml/badge.svg)](https://github.com/tracelore/wherefore/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

Data diffing tools tell you *that* 40 rows mismatched in `created_at`.
None of them tell you *why*. `wherefore` is the layer on top of a diff
that answers that question — in plain English, with real example rows
cited — and honestly says "I don't recognize this pattern" rather than
confidently guessing wrong.

**Free and key-free out of the box.** No account, no server, no API
key required for the diffing and pattern-matching — only the optional
AI narrative (`--explain`) talks to an external API, and that's off by
default.

```bash
pip install wherefore
wherefore compare old_export.csv new_export.csv
```

```
$ wherefore compare old_export.csv new_export.csv --explain
Calling Claude for 1 cluster(s)...
Compared 5 source rows against 5 target rows.
Matched rows: 5
  hire_date: 5 mismatches, matches 'timezone_shift' (confidence 1.00)
    AI: Every affected row is shifted forward by exactly 5 hours,
    consistent with a UTC-vs-local-time mismatch introduced during
    the export. Likely cause: the source system's timestamps were
    re-interpreted in the wrong timezone during migration.

Full report written to report.md
```

That's a real run, real output — not a mockup. Try it on your own
files in two minutes: see [Quickstart](#quickstart).

**New here and want the conceptual story before the code?**
[`DESIGN.md`](./DESIGN.md) walks through what `wherefore` actually
does and why it's built this way — no need to read source first.

---

## Contents

- [wherefore](#wherefore)
  - [Contents](#contents)
  - [Quickstart](#quickstart)
  - [Why this exists](#why-this-exists)
  - [What's built](#whats-built)
  - [Architecture](#architecture)
  - [Evals — why trust the explanations?](#evals--why-trust-the-explanations)
  - [Usage](#usage)
    - [One file pair](#one-file-pair)
    - [Database sources](#database-sources)
    - [A whole migration, not one table](#a-whole-migration-not-one-table)
    - [What you get without an API key](#what-you-get-without-an-api-key)
    - [What `--explain` adds](#what---explain-adds)
  - [Privacy \& data handling](#privacy--data-handling)
  - [All flags](#all-flags)
  - [Performance \& scale](#performance--scale)
  - [Troubleshooting](#troubleshooting)
  - [Contributing](#contributing)
  - [License](#license)

---

## Quickstart

**Install it:**

```bash
pip install wherefore
```

Or via Homebrew, if you'd rather not think about Python at all:

```bash
brew tap tracelore/wherefore
brew install tracelore/wherefore/wherefore
```

(Homebrew installs a prebuilt bottle on Apple Silicon Macs running a
matching macOS version — no compiling, just a normal install. On other
platforms it builds from source, which works but takes a few minutes
the first time.)

**Run it on your own files:**

```bash
wherefore compare old_export.csv new_export.csv --output report.md
```

**Want to work on `wherefore` itself instead?** Clone it and use the
dev setup script, which also runs the full test suite so you know your
environment is good before you start:

```bash
git clone https://github.com/tracelore/wherefore.git
cd wherefore
./dev_setup.sh
```

This creates a `.venv/`, installs everything, and runs the test suite
(should show **385 passed**, no API key needed — the test suite uses a
fake AI provider, zero network calls). Safe to re-run.

Works the same with `.csv`, `.json`, `.parquet`, or `.xlsx`/`.xls` —
mix and match freely, format is auto-detected per file. No key column
needed; `wherefore` finds one. If it picks wrong, or you have many
table pairs to check at once (a real migration is dozens of tables,
not one), see [usage details](#usage) below.

**Don't have a Parquet or Excel file handy?** Make one from the CSVs
above in two lines, run inside the same activated `.venv` from
`dev_setup.sh` (so `pandas`/`pyarrow` are already available) — note
`parse_dates=[...]` on any datetime column, since Parquet and Excel
store dates natively and pandas needs to know which column is one
before writing (without it, the column round-trips as plain text and
date-based patterns like `timezone_shift` won't be detected —
confirmed by testing this exact gap):

```bash
python3 -c "
import pandas as pd
df = pd.read_csv('old_export.csv', parse_dates=['hire_date'])  # name your actual date column
df.to_parquet('old_export.parquet', index=False)
df.to_excel('old_export.xlsx', index=False)
"
wherefore compare old_export.parquet new_export.parquet   # or .xlsx
```

Want the AI explanation, not just the statistical match?

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
wherefore compare old_export.csv new_export.csv --explain
```

Sensitive-looking values (emails, SSNs, card numbers, phone numbers)
are redacted before anything is sent — on by default, see
[Privacy & data handling](#privacy--data-handling).

<details>
<summary>Manual setup, if you'd rather not run the script</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
pytest tests/ -v
```

**Requires Python 3.10+.** Tested on 3.10–3.12. On very recent Python
(3.14+), pandas/numpy are compatible, but if `pip install` fails, a
smaller transitive dependency without 3.14 wheels yet is the likely
cause — try 3.11/3.12 if you hit this.
</details>

## Why this exists

Imagine two boxes of identical LEGO sets. Someone copied box A into
box B, but a few pieces are missing or the wrong color. Most tools
that check this say: *"12 pieces are different."* That's it.

`wherefore` looks at those 12 differences and says: *"These aren't
random — every one has the same color swapped the same way, consistent
with a colorblind sort. That's your root cause."* It explains the
pattern behind the differences, not just the differences themselves.

To know if it's actually doing this well — not just sounding plausible
— we build our own "messed-up" datasets on purpose, with a known,
labeled answer, and grade whether the tool finds it. That's the
[eval harness](#evals--why-trust-the-explanations), and it's
first-class here, not an afterthought.

This is not a thin prompt wrapper around an LLM. The AI sits behind a
deterministic clustering and statistical-signature step, and every
accuracy claim below is backed by that eval harness against labeled
ground truth — not vibes.

## What's built

🚧 Actively built in public. The full pipeline is real, end-to-end:
statistical detection, AI explanation, and a scored eval harness.

| | |
|---|---|
| **Formats** | CSV, JSON, Parquet, Excel — local or `s3://`, auto-detected, mix-and-match |
| **Database** | SQLite and PostgreSQL (`db://table_name` + `--source-conn-env`/`--target-conn-env`) — MySQL not yet |
| **Modes** | One pair (`compare`) or a whole batch (`compare-dir`) — files or databases |
| **Taxonomy** | 8 failure patterns built & tested: `timezone_shift`, `truncation`, `enum_drift`, `null_type_coercion`, `float_precision`, `encoding_mismatch`, `dedup_failure`, `key_mismatch` |
| **AI layer** | Verified against the real Claude API twice — manually and via the scored eval harness — 100% match on a small (seven-fixture) sample |
| **Privacy** | Redacts emails/SSNs/cards/phones before any `--explain` call, on by default |
| **Tests** | 385 passing, including a real (mocked) S3 round-trip, a real on-disk SQLite database, a real PostgreSQL server (via PGlite), and end-to-end runs against real generated files |

`dedup_failure` and `key_mismatch` are structurally different from the
other six — `dedup_failure` detects duplicated rows (re-inserted with
a new key, not the same key twice); `key_mismatch` detects a row whose
join key was reformatted (`EMP-1001` vs `EMP1001`) so it never matched
at all. Both show up as extra/missing rows rather than a column-level
mismatch, both have their own clustering path
(`detect_row_presence_patterns`), and both are verified by real,
dedicated tests — including a regression test confirming they don't
false-positive on each other's fixtures, and a regression test for a
real false positive caught while building `key_mismatch` (two
genuinely unrelated keys sharing a domain's ID prefix scored close
enough on a similarity heuristic to need a deterministic check
instead — see `TAXONOMY.md`). Neither is yet wired into the automated
eval harness above (that harness currently only scores column-mismatch
patterns) — tracked honestly as a gap, not hidden.

**Database connectivity is real for SQLite and PostgreSQL, not yet for
MySQL.** `db://table_name` reads a connection string from an
environment variable (never the command line itself, so credentials
never end up in argv or shell history) and auto-detects the table's
real primary key from its own schema metadata — shown to you for
confirmation before anything runs, since a wrong guess against a real
database is a more serious mistake than a wrong guess against a CSV.
See [Database sources](#database-sources) below. PostgreSQL support is
verified against a real running Postgres server (via PGlite, not
mocked) — primary-key detection (including composite keys), null
handling, and native timestamp typing all confirmed directly, the same
standard SQLite's tests hold. MySQL connection strings are recognized
but raise a clear "not yet implemented" error if you actually try to
connect — tracked, not silently broken.

**Not built yet:** wiring `dedup_failure`/`key_mismatch` into the eval
harness, more fixture coverage at scale, and MySQL connectivity.
SQLite, PostgreSQL, local files, and `s3://` — CSV/JSON/Parquet/
Excel — are all supported today. See [`TAXONOMY.md`](https://github.com/tracelore/wherefore/blob/main/TAXONOMY.md) for
the current pattern list and what's planned next.

<details>
<summary>The harder bugs this surfaced, if you're curious</summary>

Building the 4th pattern (`null_type_coercion`) surfaced three real
bugs spanning the comparison engine, the file loaders, and the eval
harness itself. Building the 5th (`float_precision`) surfaced a
subtler one: a magnitude-based heuristic that looked right scored a
real false positive on an adversarial test case, fixed by checking the
underlying mechanism (an exact float32 round-trip) directly instead of
approximating its size. Full account, including how each was found and
fixed: [`TAXONOMY_TODO.md`](https://github.com/tracelore/wherefore/blob/main/TAXONOMY_TODO.md).
</details>

## Architecture

```
source file, target file        (CSV, JSON, Parquet, or Excel)
        │
        ▼
 loaders + key matching   (exact by default; --fuzzy-keys for reformatted keys)
        │
        ▼
 comparison engine        (wraps datacompy; schema-aware diffing)
        │
        ▼
 deterministic clustering  (groups mismatches; runs cheap statistical
        │                   signature checks — NO causal claims here)
        ▼
 candidate pattern(s), confidence-scored
        │
        ├─── default: stop here, statistics only (free, no API key)
        │
        ▼  with --explain
 AI reasoning layer        (Claude; redacts sensitive patterns by default;
        │                   writes the causal narrative, cites real rows,
        │                   honestly flags "unrecognized" when nothing fits)
        ▼
 Markdown report           (statistics always; AI narrative alongside
                             the evidence, never instead of it)
```

**Failure patterns are data, not code.** Each one is a YAML file under
`src/wherefore/taxonomy/patterns/`, validated against a strict schema.
Adding a new pattern means writing a YAML file and a small corruptor
function — never touching clustering or reasoning code. See
[`CONTRIBUTING.md`](https://github.com/tracelore/wherefore/blob/main/CONTRIBUTING.md) for the contract.

**Clustering and reasoning are deliberately separated.** Clustering
only ever produces statistical observations ("these 12 rows differ by
exactly 5 hours"). Causal attribution ("this is a timezone bug") is
the AI's job, every time — if clustering started asserting causes, the
AI layer would become decorative and the evals would stop measuring
anything meaningful.

## Evals — why trust the explanations?

Because we control the ground truth. The synthetic data generator
creates clean datasets, then deliberately corrupts them using a known
failure pattern — recording exactly what it did in a committed
`ground_truth.json`. The eval harness runs the real pipeline against
these labeled fixtures and scores the result as precision/recall per
pattern, tracking "correctly said unrecognized" separately from
"confidently named the wrong pattern" — very different failure modes a
naive right/wrong scorer would conflate.

**Statistical mode, free, no API key, against all 7 fixtures:**

```
$ python3 -m evals.harness.run_eval
Total cases: 7
Overall accuracy (correct match + honest abstain): 100.00%

  encoding_mismatch: precision=1.00 recall=1.00
  enum_drift: precision=1.00 recall=1.00
  float_precision: precision=1.00 recall=1.00
  null_type_coercion: precision=1.00 recall=1.00
  timezone_shift: precision=1.00 recall=1.00
  truncation: precision=1.00 recall=1.00
```

**LLM mode** (`python3 -m evals.harness.run_eval --llm`, real API
calls, scores the AI's final answer instead of clustering's raw
statistics) — **also 100%**, including the one fixture designed to
test something the statistics alone can't: a cluster that legitimately
matches two patterns at once, where the AI correctly picked the right
one by reasoning about the actual values, not by defaulting to
whichever candidate came first.

Both are reproducible — clone the repo, run the commands yourself.
Seven fixtures proves the *mechanism* works end-to-end against the real
API; it doesn't prove either layer is bulletproof at scale. That's the
honest caveat, and expanding fixture coverage is the tracked next step
in [`TAXONOMY_TODO.md`](https://github.com/tracelore/wherefore/blob/main/TAXONOMY_TODO.md).

<details>
<summary>The multi-candidate case, if you want the detail</summary>

`null_type_coercion` and `enum_drift` can legitimately both match the
same cluster — a null consistently coerced to one sentinel string is,
statistically, also a "consistent value mapping." Clustering reports
both honestly rather than guessing which is "more right," since that's
a causal judgment that belongs to the AI layer, not clustering. The
eval harness scores this correctly too: a true pattern counts as found
if it appears anywhere among the reported candidates, not only if it's
listed first. Full story of how this was found and fixed:
[`TAXONOMY_TODO.md`](https://github.com/tracelore/wherefore/blob/main/TAXONOMY_TODO.md).
</details>

## Usage

### One file pair

```bash
wherefore compare old_export.csv new_export.csv --key employee_id
```

`--key` is optional — omit it and `wherefore` looks for a column that
looks like an identifier (mostly-unique values, often named with "id"
or "key"). If the same record has a differently-formatted key on each
side (e.g. `EMP-1001` vs `EMP1001`, common after a migration), add
`--fuzzy-keys`.

Files can live in S3, not just on disk — mix and match freely:

```bash
pip install "wherefore[s3]"   # boto3 is optional, only needed for s3:// paths
wherefore compare s3://old-bucket/accounts.csv s3://new-bucket/accounts.csv
```

Uses the standard AWS credential chain (env vars, `~/.aws/credentials`,
IAM role, `AWS_PROFILE`) — `wherefore` doesn't invent its own.

### Database sources

[#database-sources](#database-sources)

`wherefore` can read directly from SQLite or PostgreSQL — MySQL
connection strings are recognized but not yet connectable (a clear
"not yet implemented" error, not a silent failure):

```bash
export SOURCE_DB="sqlite:////absolute/path/to/old_export.sqlite"
export TARGET_DB="postgresql://user:password@host:5432/mydb"
wherefore compare db://accounts db://accounts \
    --source-conn-env SOURCE_DB --target-conn-env TARGET_DB
```

Two things are deliberate, not incidental:

- **The connection string lives in an environment variable, never on
  the command line.** `--source-conn-env SOURCE_DB` passes the NAME of
  an env var, not the connection string itself — so a password never
  ends up in `argv`, shell history, or a process list.
- **A wrong key against a real database is worse than a wrong key
  against a CSV.** Without `--key`, `wherefore` auto-detects the
  table's real primary key from the database's own schema, shows it
  to you, and waits for confirmation before running anything. Pass
  `--yes` to skip the prompt once you trust the detection (e.g. in a
  script), or `--key` to specify one explicitly and skip the prompt
  entirely.

PostgreSQL needs the optional `psycopg2` driver:

```bash
pip install "wherefore[db]"
```

Mixing a database source with a file is fine — comparing a live table
against a CSV export, for example:

```bash
wherefore compare db://accounts old_export.csv --source-conn-env SOURCE_DB --key account_id
```

A composite (multi-column) primary key is detected and shown, but
can't be used directly as a join key yet — pass `--key` with a single
column name to proceed, or treat this as a sign the table needs more
than `wherefore` currently models for that comparison.

SQLite connection strings follow the standard convention (the same one
SQLAlchemy uses): `sqlite:///relative/path.sqlite` for a relative path,
`sqlite:////absolute/path.sqlite` for absolute (note the 4th slash is
the path's own leading slash, not an extra separator). PostgreSQL
connection strings accept query parameters that get passed straight
through to the driver, e.g. `?sslmode=require` for a connection that
needs SSL, or `?sslmode=disable` for one that explicitly shouldn't try
to negotiate it:

```bash
export SOURCE_DB="postgresql://user:password@host:5432/mydb?sslmode=require"
```

### A whole migration, not one table

```bash
$ wherefore compare-dir old_exports new_exports --output-dir reports
Found 3 matching file pair(s). Comparing...

  [DIFF] accounts.csv: 1 finding(s) (timezone_shift)
  [DIFF] patients.csv: 1 finding(s) (truncation)
  [OK] transactions.csv: no mismatches

Done: 3 compared, 0 skipped. Reports written to reports/
```

Files are matched by **identical filename** between the two
directories — no fuzzy matching at the file level, since guessing
wrong about *which two tables* you're comparing is worse than guessing
wrong about a row key. A pair that can't be compared (bad format, no
detectable key) is skipped and reported, not fatal to the rest of the
batch. Every `compare` flag works here too, applied to every pair.

`compare-dir` also works against two whole **databases** — pass
`db://*` on both sides instead of two directories, and every table
present in both gets compared:

```bash
$ export SOURCE_DB="postgresql://user:pass@host/old_db"
$ export TARGET_DB="postgresql://user:pass@host/new_db"
$ wherefore compare-dir db://* db://* --source-conn-env SOURCE_DB --target-conn-env TARGET_DB

Detecting primary keys for 2 table(s)...
  accounts: detected primary key 'account_id'.
  orders: detected primary key 'order_id'.

Proceed comparing 2 table(s) with the keys shown above? [y/N]: y
Found 2 matching table pair(s). Comparing...

  [DIFF] accounts: 1 finding(s) (timezone_shift)
  [OK] orders: no mismatches

Done: 2 compared, 0 skipped. Reports written to reports/
```

Every table's primary key is detected from the database's own schema
and shown **once**, as a combined list, with a single confirmation for
the whole batch — not one prompt per table (which would defeat the
point of batch mode) and not silent (which would defeat the point of
the confirmation existing at all). `--yes` skips it for scripted use; a
table with no usable key is skipped individually, not fatal to the
batch. Mixing a directory with a database (one side a folder, the
other `db://*`) isn't supported yet — pairing a table name against a
filename isn't a well-defined rule, and gets its own clear error
rather than a guess.

### What you get without an API key

Real diffing, real grouping, real pattern matching — and a confidence
score that's a genuine deterministic measurement (e.g. "every
mismatched value differs by exactly the same 5-hour delta"), not an AI
guess. If nothing in the taxonomy matches, `wherefore` says
`pattern unrecognized` rather than forcing one.

### What `--explain` adds

The plain-English *why*, shown **alongside** the statistical evidence
it was reasoned from, not instead of it — so you can check the claim
yourself. In testing, the AI correctly identified a genuinely random,
non-matching corruption and proposed real alternative hypotheses (a
bad join, a mis-wired column) instead of inventing a pattern that
wasn't there.

## Privacy & data handling

`--explain` sends mismatched cell values to the Claude API. Before
that happens, values are checked against a redaction layer — emails,
SSNs, credit card numbers, US phone numbers — **on by default, no flag
needed.** Anything masked is called out in the output
(`Redacted before sending to Claude: email`). Disable with
`--no-redact` if you've already vetted your data.

Be precise about scope: this is pattern-based detection of
*structurally recognizable* sensitive data, not a general PII scanner
— it won't know that a name or a home address is sensitive. Full
detail, including a documented false-positive case found during
testing (long numeric IDs can resemble card numbers):
[`SECURITY.md`](https://github.com/tracelore/wherefore/blob/main/SECURITY.md).

## All flags

<details>
<summary>Expand</summary>

```bash
wherefore compare SOURCE TARGET [OPTIONS]

  --key TEXT                   Join key column. Auto-detected if omitted.
  --fuzzy-keys                 Allow approximate key matching (e.g. 'CUST-001' vs 'CUST001').
  --output TEXT                Where to write the report (default: report.md).
  --confidence-threshold FLOAT Minimum confidence to count as a pattern match (default: 0.9).
  --explain                    Generate plain-English AI explanations via the Claude API.
                                Requires ANTHROPIC_API_KEY. Makes real, billed API calls. Off by default.
  --no-redact                  Disable automatic redaction of emails/SSNs/cards/phones before
                                sending data to Claude with --explain. Redaction is ON by default.
  --source-conn-env TEXT       Required if SOURCE is db://table_name: the NAME of an env var
                                holding a real connection string. Never the connection string itself.
  --target-conn-env TEXT       Same as --source-conn-env, for TARGET.
  --yes / -y                   Skip the interactive confirmation prompt for an auto-detected
                                database primary key (db:// sources only).

wherefore compare-dir SOURCE_DIR TARGET_DIR [OPTIONS]

  (SOURCE_DIR/TARGET_DIR can be two directories, or db://* on both sides)
  --output-dir TEXT             Directory for one report per pair (default: reports).
  --key, --fuzzy-keys, --confidence-threshold, --explain, --no-redact   Same as `compare`, applied to every pair.
  --source-conn-env TEXT        Required if using db://* batch mode. Same as compare's flag.
  --target-conn-env TEXT        Same as --source-conn-env, for the target database.
  --yes / -y                    Skip the single combined confirmation prompt for the whole batch's
                                 auto-detected primary keys (db://* batch mode only).
```

</details>

## Performance & scale

Pressure-tested with synthetic CSV/Parquet/XLSX file pairs from 10,000
to 1,000,000 rows. Headline findings:

- **CSV and Parquet scale cleanly** through 1,000,000 rows — no
  crash, no OOM, no correctness issues. Parquet is consistently
  faster, and the gap widens at scale.
- **XLSX works at human scale, not large-comparison scale** — the cost
  is `openpyxl` itself (CPU-intensive by design, ~50x file size in
  memory when reading), not `wherefore`. Prefer CSV or Parquet for
  large comparisons.
- **A real optimization opportunity, not yet fixed**: the
  datetime-detection heuristic on every string column costs almost as
  much as parsing the file itself — even on columns that obviously
  aren't dates.

Full methodology, hardware specs, and live numbers:
[`PERFORMANCE.md`](PERFORMANCE.md).

## Troubleshooting

Hit an error message? Most of `wherefore`'s errors are written to be
specific and actionable on their own, but
[`troubleshooting.md`](https://github.com/tracelore/wherefore/blob/main/troubleshooting.md)
collects the real, specific failure modes encountered while building
and using this tool — installation issues, file-reading gotchas, key
matching, database connections, S3, and the AI layer.

## Contributing

New to the codebase? [`ARCHITECTURE.md`](https://github.com/tracelore/wherefore/blob/main/ARCHITECTURE.md)
is the orientation doc — module layout, the end-to-end pipeline, and a
handful of real "looks like X but is actually Y" gaps worth knowing
before you assume otherwise.

Contributions are welcome, especially new taxonomy patterns. Start
with [`CONTRIBUTING.md`](https://github.com/tracelore/wherefore/blob/main/CONTRIBUTING.md) — the pattern contract, why
patterns are built corruptor-first rather than YAML-first, and the
design decisions worth knowing before you dig in.

Found a security issue? See [`SECURITY.md`](https://github.com/tracelore/wherefore/blob/main/SECURITY.md).

## License

Apache License 2.0 — see [`LICENSE`](https://github.com/tracelore/wherefore/blob/main/LICENSE). Contributions are
accepted under the same license (see `NOTICE` for attribution).
