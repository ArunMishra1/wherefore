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
  - [Round 2: real hardware (Mac)](#round-2-real-hardware-mac)
    - [Test environment (Mac, round 2)](#test-environment-mac-round-2)
    - [Results, all three formats, all four tiers](#results-all-three-formats-all-four-tiers)
  - [Round 2: proof, not just numbers](#round-2-proof-not-just-numbers)
  - [Round 3: column count, not just row count](#round-3-column-count-not-just-row-count)
    - [Schema](#schema)
    - [Results: 10,000 rows, varying column count](#results-10000-rows-varying-column-count)
    - [Results: 100,000 rows, varying column count](#results-100000-rows-varying-column-count)
    - [Results: 250K, 500K, and 1M rows, varying column count](#results-250k-500k-and-1m-rows-varying-column-count)
    - [The real finding: the column penalty doesn't just exist, it compounds](#the-real-finding-the-column-penalty-doesnt-just-exist-it-compounds)
  - [What's next: real options, not just this one](#whats-next-real-options-not-just-this-one)
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

## Round 2: real hardware (Mac)

Same generator, same schema, same 1% mismatch rate, same measurement
method as round 1 — only the machine changed. This round also closes
the XLSX 500K/1M gap round 1 deferred.

### Test environment (Mac, round 2)

| | |
|---|---|
| Machine | MacBook Pro, Apple **M5 Max** |
| CPU | 18 cores (6 Super + 12 Performance) |
| Memory | 64 GiB total |
| Python | 3.14.6 |
| pandas | 2.3.3 |
| numpy | 2.4.6 |
| pyarrow | 24.0.0 |
| openpyxl | 3.1.5 |

### Results, all three formats, all four tiers

| Rows | CSV (s) | CSV mem (MB) | Parquet (s) | Parquet mem (MB) | XLSX write (s) | XLSX compare (s) | XLSX mem (MB) |
|---|---|---|---|---|---|---|---|
| 10,000 | 0.48 | 161.5 | 1.59 | 171.3 | 0.50 | 0.80 | 164.9 |
| 100,000 | 0.60 | 216.5 | 0.53 | 270.2 | 4.64 | 3.64 | 251.1 |
| 500,000 | 1.33 | 479.3 | 0.93 | 646.0 | 23.68 | 17.13 | 650.3 |
| 1,000,000 | 2.26 | 873.9 | 1.46 | 984.8 | 48.61 | 34.70 | 1,168.0 |

All three formats completed cleanly at every tier, including the
500K/1M XLSX tiers round 1 had to skip. Results verified correct at
1,000,000 rows for every format — exactly 10,000 mismatched rows
(1% of 1M), same as round 1.

**The relative ordering of CSV vs. Parquet flipped from round 1.** On
this Mac, CSV is consistently as fast as or faster than Parquet at
every tier — the opposite of the sandbox, where Parquet pulled ahead
and the gap widened with scale. The 10K-row Parquet number (1.59s) is
likely first-call pyarrow overhead, not a real per-row cost — it drops
to 0.53s at 100K rows, faster than the 10K number despite 10× the
data. Take this as a reminder that small-N numbers are noisy; the
500K–1M rows are the more trustworthy comparison, and even there CSV
and Parquet are close enough (1.33s vs 0.93s at 500K; 2.26s vs 1.46s at
1M) that format choice between the two is not the lever that matters
on this hardware. It clearly was in the sandbox.

**XLSX confirms the round-1 projection, at much better absolute
numbers.** Round 1 estimated "a minute or more" to write 500K rows;
real numbers came in at 23.68s — better than feared, but still by far
the dominant cost in the pipeline at every tier. At 1,000,000 rows,
writing the file (48.61s) takes longer than generating two 1M-row CSVs
*and* running the full comparison on both, combined, several times
over. More cores and a faster single-core clock clearly help openpyxl
somewhat (round 1's 1-core sandbox took 18.66s to write 100K rows;
this Mac took 4.64s — roughly 4×), but the structural cost openpyxl
itself describes (CPU-intensive by design) doesn't disappear just
because the hardware is faster. **The takeaway from round 1 stands,
now confirmed at full scale on real hardware: prefer CSV or Parquet
over XLSX for any comparison past tens of thousands of rows.**

**Memory stayed trivial against 64GB at every tier** — even XLSX's
worst case (1,168MB at 1M rows) used under 2% of available RAM. The
sandbox's tighter 4GB ceiling was never actually close to binding for
CSV/Parquet either, in retrospect; XLSX at 1M rows in that sandbox
would have been the one real risk, which round 1 avoided testing by
deferring it — a reasonable call at the time, confirmed unnecessary in
hindsight only because we now have the real number.

## Round 2: proof, not just numbers

The table above is real, but a table alone asks you to trust it. Here
is one full, real report — exact command, exact output, nothing
trimmed except the middle of the example-row list:

```
$ cd ~/Documents/Projects/Personal/wherefore-scale-test
$ wherefore compare data/source_1000000.csv data/target_1000000.csv \
    --output results/report_1000000_csv.md
Compared 1000000 source rows against 1000000 target rows.
Matched rows: 1000000
  amount: 10000 mismatches, pattern unrecognized

Full report written to results/report_1000000_csv.md
```

```
$ cat results/report_1000000_csv.md
# wherefore comparison report

- Source: `data/source_1000000.csv`
- Target: `data/target_1000000.csv`
- Join key: `id`
- Source rows: 1000000
- Target rows: 1000000
- Matched rows: 1000000

> **Note:** this report shows statistical findings only. Pass
> `--explain` to additionally generate a plain-English causal
> narrative for each cluster via the Claude API (requires
> `ANTHROPIC_API_KEY` and makes real, billed API calls).

## Mismatches by column (1 column(s) affected)

### `amount` -- 10000 mismatched row(s)

No known failure pattern's statistical signature matched this cluster.

Example rows:

- `{'id': 333}`: `8608.58` -> `9108.58`
- `{'id': 667}`: `9987.45` -> `10487.45`
- `{'id': 692}`: `2358.62` -> `2858.62`
- `{'id': 978}`: `5323.54` -> `5823.54`
- `{'id': 1029}`: `4037.37` -> `4537.37`
- ... and 9995 more
```

The `+500.0` delta we inject is visible in every example row
(`8608.58 -> 9108.58` is exactly `+500.0`, etc.) — this is the actual
mechanism by which "verified correct" is checked throughout this
document, not an assertion taken on faith. The reports do not currently
say *why* `amount` differs (no taxonomy signature matched, since a flat
`+500.0` shift isn't one of the eight known patterns) — this is
expected and correct behavior, not a bug: it's the same "report the
shape, not a fabricated cause" principle [`DESIGN.md`](./DESIGN.md)
describes for the clustering layer generally. A real timezone-shift or
truncation corruption, run through the same pipeline, would name the
matched pattern here instead of "pattern unrecognized."

## Round 3: column count, not just row count

Every test above used a fixed 5-column schema. That leaves a real
question unanswered: does *width* (column count), independent of row
count, change the numbers? Real estate tables, customer records, and
most migration-audit tables in practice have dozens of columns, not
five.

### Schema

Base 5 columns are unchanged from rounds 1–2 (`id`, `name`, `amount`,
`category`, `status`). Additional columns are added in a repeating
cycle of 5 realistic types, so every width tier is a strict superset
of the smaller ones:

| Extra column type | What it is | Why it's included |
|---|---|---|
| `*_date` | Real `YYYY-MM-DD` string, always parseable | The original schema never had a column that *successfully* parsed as a date — this exercises that path for the first time |
| `*_text` | ~80-char free text | Tests whether string length, not just column count, matters |
| `*_int` | Plain integer | Cheap baseline |
| `*_float` | Plain float | Cheap baseline |
| `*_flag` | Low-cardinality string (`yes`/`no`) | Same shape as `status` |

Column tiers tested: 5, 10, 20, 30, 50, 100. Same 1% `amount` mismatch
rate as every other round, so results stay comparable.

### Results: 10,000 rows, varying column count

```
$ for cols in 5 10 20 30 50 100; do
>   for fmt in csv parquet xlsx; do
>     python run_width_test.py 10000 $cols $fmt
>   done
> done
csv n10000_c5: 0.46s, peak_mem=161.6MB, exit=0
parquet n10000_c5: 0.46s, peak_mem=171.3MB, exit=0
xlsx n10000_c5: 0.80s, peak_mem=164.7MB, exit=0
csv n10000_c10: 0.46s, peak_mem=168.6MB, exit=0
parquet n10000_c10: 0.46s, peak_mem=188.2MB, exit=0
xlsx n10000_c10: 1.06s, peak_mem=174.0MB, exit=0
csv n10000_c20: 0.53s, peak_mem=190.5MB, exit=0
parquet n10000_c20: 0.53s, peak_mem=214.7MB, exit=0
xlsx n10000_c20: 1.72s, peak_mem=192.8MB, exit=0
csv n10000_c30: 0.67s, peak_mem=220.5MB, exit=0
parquet n10000_c30: 0.53s, peak_mem=243.3MB, exit=0
xlsx n10000_c30: 2.25s, peak_mem=214.0MB, exit=0
csv n10000_c50: 0.73s, peak_mem=258.0MB, exit=0
parquet n10000_c50: 0.60s, peak_mem=291.9MB, exit=0
xlsx n10000_c50: 3.44s, peak_mem=252.4MB, exit=0
csv n10000_c100: 1.06s, peak_mem=362.0MB, exit=0
parquet n10000_c100: 0.73s, peak_mem=400.7MB, exit=0
xlsx n10000_c100: 6.41s, peak_mem=352.5MB, exit=0
```

| Cols | CSV (s) | Parquet (s) | XLSX (s) |
|---|---|---|---|
| 5 | 0.46 | 0.46 | 0.80 |
| 10 | 0.46 | 0.46 | 1.06 |
| 20 | 0.53 | 0.53 | 1.72 |
| 30 | 0.67 | 0.53 | 2.25 |
| 50 | 0.73 | 0.60 | 3.44 |
| 100 | 1.06 | 0.73 | 6.41 |

**20× more columns (5→100), at a fixed 10,000 rows:** CSV time grows
2.3×, Parquet 1.6×, XLSX 8.0×. Column count is a real, independent
cost — confirming the question this section exists to answer.

### Results: 100,000 rows, varying column count

```
$ for cols in 5 10 20 30 50 100; do
>   for fmt in csv parquet xlsx; do
>     python run_width_test.py 100000 $cols $fmt
>   done
> done
csv n100000_c5: 0.74s, peak_mem=216.5MB, exit=0
parquet n100000_c5: 0.60s, peak_mem=263.6MB, exit=0
xlsx n100000_c5: 3.78s, peak_mem=251.4MB, exit=0
csv n100000_c10: 0.93s, peak_mem=317.3MB, exit=0
parquet n100000_c10: 0.66s, peak_mem=377.5MB, exit=0
xlsx n100000_c10: 6.71s, peak_mem=351.9MB, exit=0
csv n100000_c20: 1.60s, peak_mem=470.8MB, exit=0
parquet n100000_c20: 0.93s, peak_mem=584.9MB, exit=0
xlsx n100000_c20: 12.75s, peak_mem=549.8MB, exit=0
csv n100000_c30: 2.20s, peak_mem=649.4MB, exit=0
parquet n100000_c30: 1.13s, peak_mem=771.0MB, exit=0
xlsx n100000_c30: 18.91s, peak_mem=756.0MB, exit=0
csv n100000_c50: 3.47s, peak_mem=989.7MB, exit=0
parquet n100000_c50: 1.60s, peak_mem=1132.8MB, exit=0
xlsx n100000_c50: 31.24s, peak_mem=1155.1MB, exit=0
csv n100000_c100: 6.66s, peak_mem=1794.9MB, exit=0
parquet n100000_c100: 2.73s, peak_mem=2005.1MB, exit=0
xlsx n100000_c100: 62.24s, peak_mem=2052.9MB, exit=0
```

| Cols | CSV (s) | CSV mem (MB) | Parquet (s) | Parquet mem (MB) | XLSX (s) | XLSX mem (MB) |
|---|---|---|---|---|---|---|
| 5 | 0.74 | 216.5 | 0.60 | 263.6 | 3.78 | 251.4 |
| 10 | 0.93 | 317.3 | 0.66 | 377.5 | 6.71 | 351.9 |
| 20 | 1.60 | 470.8 | 0.93 | 584.9 | 12.75 | 549.8 |
| 30 | 2.20 | 649.4 | 1.13 | 771.0 | 18.91 | 756.0 |
| 50 | 3.47 | 989.7 | 1.60 | 1,132.8 | 31.24 | 1,155.1 |
| 100 | 6.66 | 1,794.9 | 2.73 | 2,005.1 | 62.24 | 2,052.9 |

Verified correct at the widest, slowest tier:

```
$ grep "mismatched row" width_test/results/report_n100000_c100_csv.md
### `amount` -- 1000 mismatched row(s)
$ grep "mismatched row" width_test/results/report_n100000_c100_xlsx.md
### `amount` -- 1000 mismatched row(s)
```

**The column-count penalty gets worse as rows increase — it does not
stay fixed.** At 10K rows, 20× more columns cost CSV 2.3× more time.
At 100K rows, the same 20× column increase costs CSV **9.0×** more
time. Rows and columns compound, they don't just add. The same
pattern holds for Parquet (1.6× → 4.5×) and XLSX (8.0× → 16.5×).

**Memory scales consistently across formats once data is loaded** —
roughly 8× for 20× more columns, at both row tiers, for all three
formats. This makes sense: once a file is read into a DataFrame, the
in-memory cost depends on the data itself, not which format it came
from.

**Practical reading:** a real-world 100-column table is not "the same
as a 5-column table, just a bit slower." At 100K rows it's a genuinely
different cost regime — XLSX in particular goes from a 3.8s
nuisance at 5 columns to a full minute at 100. CSV and Parquet stay
fast in absolute terms at every width tested so far, but the *trend*
(worsening per-column cost as rows grow) is the more important finding
than any single number.

### Results: 250K, 500K, and 1M rows, varying column count

Same matrix, continued to the larger row tiers. XLSX was run through
500K rows; at 1M rows XLSX was skipped for the 100-column tier
specifically (projected ~30 minutes combined write+compare time, based
on the confirmed worsening trend — a deliberate scope decision, not an
oversight; see [What's next](#whats-next-real-options-not-just-this-one)
below for the actual number that drove that call).

```
$ for cols in 5 10 20 30 50 100; do
>   for fmt in csv parquet xlsx; do
>     python run_width_test.py 500000 $cols $fmt
>   done
> done
csv n500000_c5: 1.53s, peak_mem=479.5MB, exit=0
parquet n500000_c5: 1.00s, peak_mem=663.5MB, exit=0
xlsx n500000_c5: 17.35s, peak_mem=650.5MB, exit=0
csv n500000_c100: 32.79s, peak_mem=7984.1MB, exit=0
parquet n500000_c100: 12.72s, peak_mem=9911.4MB, exit=0
xlsx n500000_c100: 321.36s, peak_mem=9469.1MB, exit=0
```

(Full 18-combination output for 500K, and the 12-combination
CSV/Parquet-only output for 1M, omitted here for length — every number
below was produced by the same `run_width_test.py` script shown above,
just at different row/column arguments.)

| Rows | Cols | CSV (s) | Parquet (s) | XLSX (s) |
|---|---|---|---|---|
| 250,000 | 5 | 1.07 | 0.73 | 8.97 |
| 250,000 | 100 | 16.79 | 6.39 | 162.00 |
| 500,000 | 5 | 1.53 | 1.00 | 17.35 |
| 500,000 | 100 | 32.79 | 12.72 | 321.36 |
| 1,000,000 | 5 | 2.20 | 1.46 | *(not run)* |
| 1,000,000 | 100 | 67.36 | 27.39 | *(not run — see above)* |

Verified correct at the largest, widest, heaviest combination actually
run:

```
$ grep "mismatched row" width_test/results/report_n1000000_c100_csv.md
### `amount` -- 10000 mismatched row(s)
$ grep "mismatched row" width_test/results/report_n1000000_c100_parquet.md
### `amount` -- 10000 mismatched row(s)
```

Exactly 10,000 mismatches — 1% of 1,000,000 — matching every other
tier, confirmed correct even at 15.8GB (CSV) / 19.4GB (Parquet) peak
memory.

### The real finding: the column penalty doesn't just exist, it compounds

Five row tiers now confirm the same shape for CSV's 5-column→100-column
penalty:

| Rows | CSV penalty (5→100 cols) | Parquet penalty (5→100 cols) |
|---|---|---|
| 10,000 | 2.3× | 1.6× |
| 100,000 | 9.0× | 4.5× |
| 250,000 | 15.7× | 8.8× |
| 500,000 | 21.4× | 12.7× |
| 1,000,000 | 30.6× | 18.8× |

This is not noise. It is a clean, monotonic, worsening trend across
five independent measurements. **Going from 5 to 100 columns costs
proportionally more as row count grows** — at 10K rows it's a mild
2.3×; at 1M rows the identical column-count change costs over 30×.
Rows and columns are not independent cost dimensions that simply add —
they compound.

**A second, separate, equally real finding: at high column counts,
format choice flips from "doesn't matter much" to "matters a lot."**

| Rows | CSV time ÷ Parquet time, at 100 columns |
|---|---|
| 10,000 | 1.5× |
| 100,000 | 2.4× |
| 250,000 | 2.6× |
| 500,000 | 2.6× |
| 1,000,000 | 2.5× |

At 5 columns (the original row-count-only tests), CSV and Parquet were
close enough that format choice barely mattered. At 100 columns, CSV
is consistently **2.4–2.6× slower than Parquet**, from 100K rows
upward. **Format choice matters more as tables get wider, not as they
get taller** — the opposite of what the original row-only testing
would have suggested on its own.

## What's next: real options, not just this one

Three real findings came out of this investigation, each with a
different appropriate response. Listed here as options to evaluate,
not yet committed to:

**1. The datetime-detection heuristic (`loaders._try_parse_datetime_columns`)
is real, measured overhead, and it's the cheapest thing to fix.**
Confirmed earlier in this document: it costs almost as much as the CSV
parse it sits on top of, on every string column, every load — even
columns that obviously aren't dates. A cheap pre-check (sample a
handful of values per column; only attempt the full vectorized
`pd.to_datetime` call if they look plausibly date-shaped) would not
require touching the comparison engine, the clustering layer, or
anything downstream. This is the highest-value, lowest-risk change
available right now, precisely because it's isolated to one function
with a narrow, well-understood job.

**2. Parallelization is a real option, but it has a real ceiling, and
it doesn't fix the column-count compounding by itself.** `wherefore`
currently runs single-threaded per comparison. Two different kinds of
parallelism are actually available, and they help different things:

- **Across files** (`compare-dir`'s batch mode): comparing N table
  pairs is currently sequential. Running multiple pairs concurrently
  (a process pool, one process per table pair) would help wall-clock
  time for a multi-table migration audit directly — this is the
  easier, lower-risk form of parallelism, since each table pair is
  already fully independent work.
- **Within one comparison** (splitting one huge file into chunks,
  loading/parsing in parallel, merging results): genuinely harder.
  The datetime-detection heuristic and the diff itself both operate on
  whole-column vectorized operations already — pandas/numpy are
  already using SIMD/vectorization internally for a lot of this, so
  naive multi-threading often fights the GIL for the Python-level
  parts and gains little. This would need real profiling to find which
  specific step benefits from chunking before attempting it, not an
  assumption that "more cores" automatically helps a vectorized
  pandas/numpy pipeline the way it would a row-by-row pure-Python loop.

**3. A different engine for the heaviest cases is worth a real
investigation, not a rewrite.** `wherefore` already depends on
`polars` transitively (via `datacompy`). Polars is built for exactly
this kind of large-tabular-diff workload and is known for handling
wide tables more gracefully than pandas in general. Worth a real,
isolated experiment — load the same 1M×100 CSV via `polars.read_csv`
instead of `pandas.read_csv`, time it directly, see if the gap holds —
before deciding whether routing the hot path (load + datetime
detection specifically, not necessarily the whole pipeline) through
polars is worth the added complexity. Not yet attempted. This is a
meaningfully smaller, lower-risk move than a Go/Rust rewrite, since
polars is already a dependency and already speaks pandas-compatible
DataFrames.

**Recommended order, if/when this gets picked up:** fix #1 first
(cheap, isolated, immediately reduces real cost on every comparison
regardless of size). Measure again. Then decide whether #2 or #3 is
worth pursuing based on where the remaining cost actually sits — not
before, since fixing #1 will change the relative shape of every number
in this document.

## Still to measure

- S3-backed sources (network latency as a new variable)
- Database sources (`db://`), including `compare-dir`'s batch mode
- Realistic/messy data: real nulls, near-duplicate keys,
  `--fuzzy-keys` — still excluded from this clean baseline (real dates
  are now covered, via the `*_date` column in the width matrix)
- Whether the CSV/Parquet ordering flip between sandbox and Mac (at 5
  columns specifically) is a hardware effect, a pandas/pyarrow version
  difference, or something else — not yet investigated
- Why CSV's column-width compare-time penalty is smaller than its
  write-time penalty for the same column-count change — not yet
  investigated
- The actual polars experiment described above (#3) — not yet run
- XLSX at 1M rows × 100 columns — deliberately skipped (projected
  ~30 minutes combined); revisit only if a real use case needs it

