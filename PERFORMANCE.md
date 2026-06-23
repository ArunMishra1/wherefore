# Performance & scale notes

This is a living document, updated as more pressure-test results come
in (S3 sources, database sources, larger row counts, messier/realistic
data). It exists because "does this scale" only has a real answer when
backed by actual measurements on actual data -- not because any of the
numbers here are a guarantee for every machine or every dataset shape.

## Methodology

- **Schema**: deliberately simple/clean for this first pass --
  `id` (int, join key), `name` (string), `amount` (float),
  `category` (low-cardinality string), `status` (low-cardinality
  string, `active`/`inactive`). No dates, no nulls, no fuzzy-key
  scenarios yet -- those come in a later round, once the clean
  baseline is established.
- **Mismatch rate**: exactly 1% of rows have `amount` perturbed by a
  fixed `+500.0` delta, on every row count, so every comparison has a
  real, proportional amount of diff work to do -- not a trivial
  all-rows-match comparison, which would understate the cost of the
  actual comparison step.
- **Measurement**: wall-clock time via `subprocess` + `time.time()`
  around the real `wherefore compare` CLI invocation (not a library
  call -- this includes process startup and CLI overhead, which is
  real cost a user actually pays). Peak memory via `psutil`, sampling
  the process tree (parent + children) every 50ms and keeping the max
  RSS seen, not a single end-of-run snapshot.
- **Each number is from a single run**, not averaged across repeated
  trials. Treat single-run numbers as indicative of scale and shape,
  not as precise benchmarks -- rerun before relying on an exact figure
  for a specific decision.

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

This is a resource-constrained container, not representative hardware
-- 1 CPU core and ~4GB RAM is well below a typical real workstation.
**Treat the absolute times below as round-1, sandbox-only numbers.**
What should transfer to real hardware is the *shape* of the curves
(linear vs. super-linear, which format/step dominates) -- the actual
seconds-per-row will very likely look better on a real machine with
more cores and RAM. Round 2 (a real Mac, per the working plan) will
confirm or correct this.

## Results: `wherefore compare`, single CSV/Parquet file pair

Clean/simple schema, 1% injected mismatch rate, single-threaded,
sandbox environment above.

| Rows | CSV time (s) | CSV peak mem (MB) | Parquet time (s) | Parquet peak mem (MB) |
|---|---|---|---|---|
| 10,000 | 1.24 | 158.0 | 1.22 | 169.8 |
| 100,000 | 1.76 | 197.8 | 1.34 | 208.5 |
| 500,000 | 4.10 | 350.3 | 2.13 | 358.9 |
| 1,000,000 | 8.34 | 563.9 | 3.09 | 450.1 |

Both formats completed cleanly at every size tested -- no crash, no
OOM, no hang, and the comparison results were verified correct (right
join key, right row counts, right mismatch count and example rows) at
every tier, not just the smallest.

**Parquet is consistently faster than CSV at every size, and the gap
widens with scale** (1.02x at 10K rows, 2.7x at 1M rows) -- consistent
with parquet's native typing (no datetime-detection heuristic needs to
run at all, see below) and columnar compression handling this schema's
low-cardinality string columns well.

**Per-10K-row cost actually decreases from 10K through 500K rows**,
then begins flattening toward roughly-linear between 500K and 1M --
most of the early "sub-linear" behavior is fixed per-run overhead
(process startup, library imports) being amortized over more rows,
not the underlying algorithm getting cheaper with scale. Treat 500K-1M
as the more representative long-run rate, not the 10K number.

### XLSX: write-time-dominated, scales far worse than CSV/Parquet

| Rows | XLSX write time (s, source file only) | `wherefore compare` time (s) |
|---|---|---|
| 10,000 | 1.86 | 3.25 |
| 100,000 | 18.66 | 18.67 |
| 500,000 | *(not yet run)* | *(not yet run)* |
| 1,000,000 | *(not yet run)* | *(not yet run)* |

At 100K rows, `wherefore compare`'s total time (18.67s) is almost
identical to just the raw `openpyxl` write time for one file (18.66s)
-- confirming the bottleneck is openpyxl's own read/write speed, not
anything in wherefore's logic. openpyxl's own documentation describes
itself as CPU-intensive by design (functionality prioritized over
performance) and warns of memory use around 50x the on-disk file size
when reading -- both are real, structural properties of the library,
not something wherefore's code can route around while still using
openpyxl. 500K/1M-row XLSX tiers are deliberately deferred (next
update) given the clear trend already visible in the 10K->100K jump
(write time scaled roughly 10x for a 10x row increase, from an already
slow starting point) -- expected write time alone at 500K is in the
range of a minute or more, before the comparison itself even runs.

**Practical takeaway so far: XLSX is fine for the row counts Excel
itself is comfortable with as a human-facing tool (low tens of
thousands), and a poor choice as the file format for genuinely large
migration-audit-scale comparisons.** CSV or Parquet -- ideally Parquet
-- should be the recommended format for large comparisons; this isn't
a wherefore limitation, it's an inherent property of the XLSX format
and the leading Python library for reading/writing it.

## Where the time actually goes (1,000,000-row CSV breakdown)

Profiling `wherefore compare`'s real components in isolation, on the
1M-row CSV pair, sandbox environment above:

| Step | Time (s) | Share of total |
|---|---|---|
| Raw `pd.read_csv` (both files, ~1.14s each) | ~2.30 | ~28% |
| Datetime-detection heuristic (both files, ~1.13s each) | ~2.25 | ~27% |
| `diff_engine.compare` (the actual statistical comparison) | ~0.76 | ~9% |
| Process startup, imports, report generation, remainder | ~3.0 | ~36% |

**The datetime-detection heuristic (`loaders._try_parse_datetime_columns`)
costs almost as much as the raw CSV parse it's layered on top of** --
roughly doubling load time -- and runs on every object-dtype column on
every load, including columns with no plausible relationship to dates
(e.g. a `name` column full of `"name_523891"`-style strings). The cost
is concentrated almost entirely in the vectorized `pd.to_datetime`
calls themselves (0.71s for the `name` column's 1,000,000 values
alone), not in the pure-Python `isdigit()` pre-check, which short-
circuits in microseconds since real string columns fail it on the
first character.

**By contrast, the actual statistical comparison -- the core value
this tool provides -- is the cheapest major component measured.**
There is real, concrete headroom here: a cheap upfront check (e.g.
sampling a handful of values per column to see if they look remotely
date-shaped before calling `pd.to_datetime` on the full column) could
likely recover a meaningful fraction of total load time on wide,
mostly-non-date-typed real-world tables, without changing correctness.
Not yet implemented or scoped as a fix -- noted here as a finding from
measurement, to revisit deliberately rather than fix reactively.

## Still to measure

- XLSX at 500K and 1M rows (deferred above)
- S3-backed sources (network latency as a new variable)
- Database sources (`db://`), including `compare-dir`'s batch mode
  across multiple tables
- Realistic/messy data: actual date columns, nulls, near-duplicate
  keys, fuzzy-key matching (`--fuzzy-keys`) -- all deliberately
  excluded from this clean-baseline round
- Confirmation on real hardware (a Mac, not this sandbox) to separate
  "absolute numbers specific to a constrained container" from "shape
  of the scaling curve," which should be the more portable finding
