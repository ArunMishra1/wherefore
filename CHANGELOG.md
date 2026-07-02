# Changelog

All notable changes to `wherefore` are documented here. Dates are
release dates (GitHub Release + PyPI), not commit dates.

### Contents

- [0.4.0](#040)
- [0.3.1](#031)
- [0.3.0](#030)
- [0.2.0](#020)
- [0.1.0](#010)

---

## 0.4.0

### Added

- **Column-count / schema mismatch detection** — source and target no
  longer need identical column sets. Columns present on only one side
  (dropped, renamed with no explicit mapping, or newly added) are now
  surfaced in every report as a `## Schema differences` section and a
  CLI warning, instead of being silently excluded from comparison with
  no trace. `compare-dir` batch runs get a per-pair `[SCHEMA]` status
  and a batch-level tally. No fuzzy rename-guessing is attempted — see
  `TAXONOMY.md` for why. Confirmed directly against real `datacompy`
  1.0.2: this data (`df1_unq_columns()`/`df2_unq_columns()`) was
  already being computed by the underlying comparison engine but
  discarded before this fix. Column ORDER was separately confirmed to
  already be a non-issue (matching is entirely name-keyed) — no fix
  needed there, but locked in with a regression test.

---

## 0.3.1

**Fixes a real segfault and a real correctness bug. If you're on
0.3.0, upgrade.**

### Fixed

- **Segfault in `pandas==3.0.3`** — `pd.to_datetime(..., errors="coerce",
  format="ISO8601")` crashed the entire process (not a Python
  exception — a real `SIGSEGV`) when called on an ordinary non-date
  string column, which is exactly what every CSV/database load does
  while detecting real date columns. Confirmed by a minimal,
  `wherefore`-free reproduction; confirmed fixed under `pandas==2.2.3`
  with the identical call. Dependency constraint changed from
  `pandas>=2.2` to `pandas>=2.2,<3.0` until a confirmed-fixed pandas
  3.x release is available.
- **`--fuzzy-keys` could merge unrelated records** — two genuinely
  different rows whose keys differ only in separator type or case
  (e.g. `"EMP-900000"` vs `"EMP_900000"` as two different employees)
  could score high enough to be treated as the same record, since key
  strings alone carry no information to tell "same record, reformatted"
  apart from "different record, coincidentally similar key." Fixed
  with a new content sanity-check that compares the matched rows'
  actual non-key values before accepting a fuzzy match, deferring
  rather than guessing when there isn't enough signal either way.

### Improved

- **Datetime-detection heuristic is now pre-checked on a small random
  sample** before committing to a full-column parse — confirmed by
  direct measurement that the heuristic previously cost almost as
  much as parsing the file itself, even on columns with no
  relationship to dates. Real, measured improvement: up to ~18× faster
  on the isolated step, ~33% faster end-to-end on tables without many
  real date columns; smaller but still real on tables that do have
  genuine date columns, since those still need the real conversion.

### Added

- `PERFORMANCE.md` — a living document of real, reproducible
  pressure-test results across file formats (CSV/Parquet/XLSX),
  column counts, database sources (SQLite/PostgreSQL, including
  `compare-dir`'s batch mode), S3 sources, `--fuzzy-keys` at scale,
  and messy/realistic data (nulls, sentinel coercion, near-duplicate
  keys). Includes hardware specs, exact commands, and real output —
  not just summary numbers.
- `DESIGN.md` — a narrative explainer of what `wherefore` does and why
  it's built this way, for a reader who wants the idea before (or
  instead of) the code.

### Internal

- Added regression tests for the datetime pre-check (including the
  adversarial case of null sentinels clustered at the start of a
  column) and the fuzzy-key content sanity-check (including two real
  edge cases found while building the fix: a strict-majority threshold
  that wrongly rejected a legitimate reformat-plus-real-mismatch case,
  and unreliable cardinality detection on small DataFrames).

---

## 0.3.0

### Added

- **Database batch comparison mode for `compare-dir`** — `db://*` on
  both sides compares every matching table pair across two databases
  in one run, the database equivalent of comparing every matching file
  in two directories. Supports SQLite and PostgreSQL.

---

## 0.2.0

### Added

- **Real PostgreSQL connectivity** — verified against an actual
  PostgreSQL server (not mocked), not just SQLite.
- `ARCHITECTURE.md` and `troubleshooting.md`.

### Fixed

- Logo path corrected to an absolute URL so it renders correctly on
  PyPI's project page (relative paths don't resolve there the way they
  do on GitHub).

---

## 0.1.0

Initial public release.

### Added

- `compare` — CSV, JSON, Parquet, and Excel file comparison with
  automatic or explicit join-key detection, fuzzy key matching
  (`--fuzzy-keys`), and a taxonomy of deterministic failure-pattern
  signatures (time-zone shift, truncation, enum drift, null coercion,
  float precision drift, encoding mismatch, deduplication failure, key
  mismatch).
- `compare-dir` — directory-vs-directory batch comparison, matching
  files by name.
- S3 source support (`s3://` paths) for CSV/JSON/Parquet/Excel.
- Optional `--explain` layer: redacts common structured PII, then asks
  the Claude API for a plain-English narrative of each statistical
  finding — strictly after the deterministic taxonomy has already
  identified the pattern, never before.
- A real evaluation harness: synthetic data with one known corruption
  injected, ground truth kept, full pipeline scored against it.
