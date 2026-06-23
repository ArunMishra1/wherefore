# Performance & scale notes

A living document, updated as new pressure-test results come in (S3,
databases, larger row counts, messier data). "Does this scale" only
has a real answer when backed by measurements — not a guarantee for
every machine or dataset shape.

## Contents

- [Performance \& scale notes](#performance--scale-notes)
  - [Contents](#contents)
  - [Methodology](#methodology)
  - [Test environment (sandbox, round 1)](#test-environment-sandbox-round-1)
  - [Results: `wherefore compare`, single CSV/Parquet file pair](#results-wherefore-compare-single-csvparquet-file-pair)
  - [XLSX: write-time-dominated, scales far worse than CSV/Parquet](#xlsx-write-time-dominated-scales-far-worse-than-csvparquet)
  - [Where the time actually goes (1,000,000-row CSV breakdown)](#where-the-time-actually-goes-1000000-row-csv-breakdown)
  - [Still to measure](#still-to-measure)

---

## Methodology

- **Schema**: simple and clean for this first pass — `id` (int, join
  key), `name` (string), `amount` (float), `category` and `status`
  (low-cardinality strings). No dates, no nulls, no fuzzy keys yet —
  those come once the clean baseline is established.
- **Mismatch rate**: exactly 1% of rows have `amount` perturbed by a
  fixed delta, at every row count. Every comparison has real diff work
  to do, not a trivial all-match case.
- **Timing**: wall-clock via `subprocess` + `time.time()` around the
  real `wherefore compare` CLI — includes process startup, the real
  cost a user pays.
- **Memory**: peak RSS via `psutil`, sampling the process tree every
  50ms, not a single end-of-run snapshot.
- **Single run per number**, not averaged. Treat these as indicative
  of scale and shape — rerun before relying on an exact figure.

## Test environment (sandbox, round 1)

| | |
|---|---|
| CPU | Intel Xeon @ 2.80GHz, **1 core** |
| Memory | 3.9 GiB total |
| OS | Ubuntu 24.04.4 LTS, x86_64 |
| Python | 3.12.3 |
| pandas | 3.0.2 |
| numpy | 2.4.4 |
| pyarrow | 24.0.0 |
| openpyxl | 3.1.5 |

A resource-constrained container, not representative hardware — well
below a typical workstation. **Treat the absolute times below as
round-1, sandbox-only.** What should transfer to real hardware is the
*shape* of the curves, not the exact seconds. Round 2 (a real Mac)
will confirm or correct this.

## Results: `wherefore compare`, single CSV/Parquet file pair

| Rows | CSV time (s) | CSV peak mem (MB) | Parquet time (s) | Parquet peak mem (MB) |
|---|---|---|---|---|
| 10,000 | 1.24 | 158.0 | 1.22 | 169.8 |
| 100,000 | 1.76 | 197.8 | 1.34 | 208.5 |
| 500,000 | 4.10 | 350.3 | 2.13 | 358.9 |
| 1,000,000 | 8.34 | 563.9 | 3.09 | 450.1 |

Both formats completed cleanly at every tier — no crash, no OOM, no
hang. Results verified correct (right key, right counts, right
mismatches) at every size, not just the smallest.

**Parquet is consistently faster, and the gap widens with scale** —
1.02× at 10K rows, 2.7× at 1M rows. Consistent with parquet's native
typing (no datetime-detection heuristic needed at all) and columnar
compression handling these low-cardinality columns well.

**Per-10K-row cost drops from 10K through 500K**, then flattens toward
linear between 500K and 1M. Most of that early improvement is fixed
per-run overhead (startup, imports) being amortized over more rows —
not the algorithm getting cheaper. Treat 500K–1M as the more
representative long-run rate.

## XLSX: write-time-dominated, scales far worse than CSV/Parquet

| Rows | XLSX write time (s, source only) | `wherefore compare` time (s) |
|---|---|---|
| 10,000 | 1.86 | 3.25 |
| 100,000 | 18.66 | 18.67 |
| 500,000 | *(not yet run)* | *(not yet run)* |
| 1,000,000 | *(not yet run)* | *(not yet run)* |

At 100K rows, total `wherefore compare` time (18.67s) is almost
identical to just the raw `openpyxl` write time (18.66s). The
bottleneck is openpyxl itself, not `wherefore`'s logic — openpyxl's
own docs describe it as CPU-intensive by design, with ~50× the file
size in memory when reading. Those are structural properties of the
library, not something `wherefore` can route around while still using
it.

500K/1M tiers are deferred for now. The 10K→100K trend (~10× time for
10× rows, from an already-slow baseline) projects to a minute or more
just to write the 500K file.

**Takeaway: XLSX is fine at the scale Excel itself is comfortable
with — low tens of thousands of rows. It's a poor choice for large
migration-audit comparisons.** Prefer CSV or Parquet, ideally Parquet,
for anything bigger. This is a property of the XLSX format and its
leading Python library, not a `wherefore` limitation.

## Where the time actually goes (1,000,000-row CSV breakdown)

Profiling `wherefore compare`'s real components in isolation, 1M-row
CSV pair, sandbox above:

| Step | Time (s) | Share of total |
|---|---|---|
| Raw `pd.read_csv` (both files) | ~2.30 | ~28% |
| Datetime-detection heuristic (both files) | ~2.25 | ~27% |
| `diff_engine.compare` (the actual comparison) | ~0.76 | ~9% |
| Startup, imports, report generation, remainder | ~3.0 | ~36% |

**The datetime-detection heuristic costs almost as much as the parse
it sits on top of** — roughly doubling load time. It runs on every
string column, every load, including columns with no relationship to
dates (a `name` column full of `"name_523891"`-style strings). The
cost is in the vectorized `pd.to_datetime` calls themselves (0.71s for
the `name` column alone) — not the pure-Python `isdigit()` pre-check,
which short-circuits in microseconds.

**The actual statistical comparison — the core value this tool
provides — is the cheapest major component measured.** There's real
headroom here: a cheap pre-check (sample a few values per column
before calling `pd.to_datetime` on the whole thing) could likely
recover a meaningful chunk of load time on wide, mostly-non-date
tables, without changing correctness. Not yet implemented — noted here
as a finding to revisit deliberately, not fixed reactively.

## Still to measure

- XLSX at 500K and 1M rows
- S3-backed sources (network latency as a new variable)
- Database sources (`db://`), including `compare-dir`'s batch mode
- Realistic/messy data: real dates, nulls, near-duplicate keys,
  `--fuzzy-keys` — all excluded from this clean baseline
- Real hardware (a Mac, not this sandbox) — to separate "sandbox-only
  numbers" from "shape of the scaling curve"
