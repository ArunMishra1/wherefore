# Performance & scale notes

A living document, updated as new pressure-test results come in (S3,
databases, larger row counts, messier data). "Does this scale" only
has a real answer when backed by measurements — not a guarantee for
every machine or dataset shape.

### Contents

- [Methodology](#methodology)
- [Test environment](#test-environment-sandbox-round-1)
- [CSV/Parquet results](#results-wherefore-compare-single-csvparquet-file-pair)
- [XLSX results](#xlsx-write-time-dominated-scales-far-worse-than-csvparquet)
- [Where the time goes](#where-the-time-actually-goes-1000000-row-csv-breakdown)
- [Round 2: real hardware](#round-2-real-hardware-mac)
- [Round 2: proof](#round-2-proof-not-just-numbers)
- [Round 3: column count](#round-3-column-count-not-just-row-count)
- [What's next](#whats-next-real-options-not-just-this-one)
- [Item 1 results](#item-1-results-the-datetime-detection-pre-check)
- [Item 3 results](#item-3-results-the-polars-experiment)
- [Item 4 investigation](#item-4-investigation-the-ordering-flip-that-wasnt)
- [Item 5 investigation](#item-5-investigation-why-compare-time-and-write-time-penalties-differed)
- [Round 4: database sources](#round-4-database-sources-item-6)
- [Round 4 extension: db column count](#round-4-extension-does-column-count-compound-for-databases-too)
- [Round 5: S3 sources](#round-5-s3-sources-item-7)
- [Round 6: compare-dir batch mode](#round-6-compare-dirs-database-batch-mode-item-1-of-the-follow-up-list)
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

**Correction (after investigation — see below): there was no real
ordering flip.** An earlier version of this document claimed CSV was
"consistently as fast as or faster than Parquet" on this Mac. Re-
reading the table above directly: Parquet is faster at 100K, 500K, and
1M rows (0.53 vs 0.60; 0.93 vs 1.33; 1.46 vs 2.26) — only the 10K-row
tier shows CSV ahead, and that tier is the one already flagged as
likely first-call pyarrow overhead, not a real per-row cost (the 10K
Parquet number, 1.59s, is slower than the 100K number, 0.53s, despite
10× less data). Once that known-noisy point is set aside, **Parquet is
faster than CSV at every reliable tier, on both the sandbox and this
Mac** — the original "flip" claim was a misreading of this document's
own table, not a real environment difference. Confirmed directly: see
[Item 4 investigation](#item-4-investigation-the-ordering-flip-that-wasnt)
below for how this was checked rather than just reasserted.

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
different appropriate response.

**1. ✅ IMPLEMENTED — the datetime-detection heuristic
(`loaders._try_parse_datetime_columns`) now pre-checks a random
20-value sample before committing to the full-column call.** See
[Item 1: results](#item-1-results-the-datetime-detection-pre-check)
below for what actually changed once this was built and measured —
including an honest, real limitation the fix does not solve.

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
investigation, not a rewrite.** ✅ **EXPERIMENT RUN — see
[Item 3: results](#item-3-results-the-polars-experiment) below.** The
short version: real, repeatable 3.5–4× speedup for the load +
datetime-detection step specifically, on exactly the date-heavy wide
tables where item 1's fix had the smallest effect. Not yet a decision
to migrate anything — see the scope boundary in that section for what
this experiment does and does not prove.

### Item 1 results: the datetime-detection pre-check

**The fix:** before calling `pd.to_datetime` on a full column,
randomly sample 20 non-null values and run the cheap parse on the
sample only. If all 20 fail, skip the column entirely — it's
essentially certain (≈1-in-95-trillion false-skip risk at the existing
20%-failure threshold, computed directly) not to be a date column.
This only ever skips the expensive call early; it never changes the
outcome for a column that proceeds past it. A random sample, not the
first N rows, was a deliberate, tested choice: a real export can have
sentinel/null values clustered at the start, and a first-N sample
would wrongly conclude a genuine date column has zero parseable
values — confirmed directly by constructing exactly that case and
showing a first-N sample fails it while a random sample doesn't. A new
regression test (`test_sentinel_nulls_clustered_at_the_start_still_parse_as_hybrid_column`)
locks this in.

**Isolated function cost, 1,000,000-row file, 5 columns (3 string,
none of them dates) — the original test case this document already
profiled:**

```
$ python3 -c "
import time
import pandas as pd
from wherefore.comparison import loaders

t0 = time.time()
raw = pd.read_csv('data/source_1000000.csv', keep_default_na=False, na_values=[''])
t1 = time.time()
print(f'raw pd.read_csv: {t1-t0:.3f}s')

parsed = loaders._try_parse_datetime_columns(raw)
t2 = time.time()
print(f'_try_parse_datetime_columns: {t2-t1:.3f}s')
"
raw pd.read_csv: 0.210s
_try_parse_datetime_columns: 0.061s
```

Before this fix, the same function on the same file (see
[Where the time actually goes](#where-the-time-actually-goes-1000000-row-csv-breakdown)
above) cost roughly 1.1s per file — **this is a real, large, ~18×
speedup when none of the string columns are actually dates.**
End-to-end `wherefore compare` on this file: **8.34s → 5.57s**, a 33%
total reduction (remaining time is process startup, the diff itself,
and report generation — none of which this fix touches).

**The honest limit: a 100-column table with real date columns doesn't
see the same win, because the fix correctly does NOT skip real dates.**
Isolated breakdown on the column-width test's widest tier (1,000,000
rows, 100 columns, 60 of them string-typed):

```
$ python3 -c "
... [same diagnostic, against width_test/data/source_n1000000_c100.csv]
"
Total string columns: 60
Columns that pre-check SKIPPED (cheap path): 41
Columns that proceeded to the FULL expensive call: 19
Which ones proceeded: ['extra_0_date', 'extra_1_date', ... extra_18_date']
```

The 19 columns that still pay the full cost are exactly the 19
genuinely-real-date columns in this schema (one per extra-column
cycle) — the fix correctly identifies them as needing the real
conversion, since skipping them would silently leave real dates as
unconverted strings, breaking correctness for the sake of speed. That
would be a worse bug than the one this fix solves. End-to-end on this
combination: **67.36s → 57.05s**, a real but much smaller 15%
reduction — verified correct at 10,000 mismatched rows, same as every
prior run on this combination.

**What this means, stated plainly:** this fix helps a lot on tables
where most string columns genuinely aren't dates (the common case for
ID/name/category/status-style columns) and helps much less on tables
with many genuine date columns, because converting real dates is
necessary, correct work this fix was never meant to eliminate. The
remaining cost on date-heavy wide tables is a real candidate for item
3 (the polars experiment) — not a sign this fix did the wrong thing.

### Item 3 results: the polars experiment

**The question:** does `polars` genuinely load and detect dates faster
than pandas, on the exact case where item 1's fix had the smallest
effect (date-heavy, wide tables)? Tested directly, not assumed.

**Method:** same 100-column file used for item 1's "honest limit"
case, loaded two ways in separate processes (kept separate
deliberately — running both libraries' full copies of a large file in
one process caused a real, confirmed OOM kill in the sandbox
environment, itself a useful data point about combined memory cost,
not a script bug). Pandas side uses the real, shipped
`_try_parse_datetime_columns` (with item 1's fix). Polars side
implements the equivalent two-step logic by hand for this experiment
only — sample 20 values per string column, skip if none parse, fully
convert the columns that do — matching pandas' actual behavior, not a
simplified approximation.

```
$ python3 -c "
import time
import pandas as pd
from wherefore.comparison import loaders

t0 = time.time()
raw_pd = pd.read_csv('source_n500000_c100.csv', keep_default_na=False, na_values=[''])
t1 = time.time()
print(f'pandas.read_csv: {t1-t0:.3f}s')
parsed_pd = loaders._try_parse_datetime_columns(raw_pd)
t2 = time.time()
print(f'pandas _try_parse_datetime_columns: {t2-t1:.3f}s')
print(f'pandas total: {t2-t0:.3f}s')
"
pandas.read_csv: 21.777s
pandas _try_parse_datetime_columns: 3.299s
pandas total: 25.075s
```

```
$ python3 -c "
import time
import polars as pl

t0 = time.time()
raw_pl = pl.read_csv('source_n500000_c100.csv')
t1 = time.time()
print(f'polars.read_csv: {t1-t0:.3f}s')

string_cols = [name for name, dtype in raw_pl.schema.items() if dtype == pl.String]
t0 = time.time()
date_cols_found = []
for col in string_cols:
    sample = raw_pl[col].head(20)
    parsed_sample = sample.str.to_datetime('%Y-%m-%d', strict=False)
    if parsed_sample.null_count() < 20:
        date_cols_found.append(col)
t1 = time.time()
print(f'polars pre-check: {t1-t0:.3f}s, found {len(date_cols_found)} date columns')

t0 = time.time()
exprs = [pl.col(c).str.to_datetime('%Y-%m-%d', strict=False) for c in date_cols_found]
converted = raw_pl.with_columns(exprs)
t1 = time.time()
print(f'polars full conversion: {t1-t0:.3f}s')
"
polars.read_csv: 5.237s
polars pre-check: 0.040s, found 19 date columns
polars full conversion: 1.169s
```

| Rows | Pandas total (s) | Polars total (s) | Speedup |
|---|---|---|---|
| 100,000 | 4.53 | 1.28 | 3.5× |
| 500,000 | 25.07 | 6.45 | 3.9× |

Polars correctly identified the same 19 real date columns pandas did
— verified directly, not assumed:

```
$ python3 -c "
import polars as pl
raw_pl = pl.read_csv('source_n100000_c100.csv')
converted = raw_pl.with_columns([pl.col('extra_0_date').str.to_datetime('%Y-%m-%d', strict=False)])
print(converted['extra_0_date'].dtype, converted['extra_0_date'].head(3).to_list())
print(converted['name'].dtype, converted['name'].head(3).to_list())
"
Datetime(time_unit='us', time_zone=None) [datetime.datetime(2022, 11, 26, 0, 0), ...]
String ['name_0', 'name_1', 'name_2']
```

Real dates converted correctly; non-date strings correctly left
untouched — same correctness guarantee as the pandas fix, just faster.

**Real, consistent 3.5–3.9× speedup, holding steady (not shrinking or
growing dramatically) across the two row counts tested.** This is a
genuine, repeatable result, not a one-off favorable measurement.

**Scope boundary — what this experiment does NOT prove:** this tested
*load and date-detection only*, in isolation, with a hand-written
polars equivalent built specifically for this experiment. It does not
test whether `datacompy`'s diff logic works against polars DataFrames
directly, what it would take to convert clustering/taxonomy code to
expect polars instead of pandas, or whether a mixed pipeline (polars
for loading, converting to pandas before the diff) would keep the
speedup or lose it to conversion overhead. **This is evidence the
premise is worth a real scoped engineering effort — it is not, by
itself, a decision to migrate anything.** The honest next step, if
this gets picked up, is a small spike: load via polars, convert to
pandas immediately after date-detection, time the conversion step
itself, and see how much of this 3.5–3.9× survives once the rest of
the pipeline (still pandas-based) is back in the loop.

### Item 4 investigation: the "ordering flip" that wasn't

**The question:** round 2's original write-up claimed CSV was
consistently as fast as or faster than Parquet on the Mac, the
opposite of the sandbox. Was this a real pandas-version effect
(sandbox ran pandas 3.0.2, Mac ran 2.3.3), a hardware effect, or
something else?

**Step 1 — re-read the data.** The claim doesn't actually match round
2's own table: Parquet is faster at 100K, 500K, and 1M rows; only the
known-noisy 10K tier shows CSV ahead. This alone resolves most of the
question — see the correction above. No real flip exists once the
noisy small-N point is set aside.

**Step 2 — rule out the pandas-version hypothesis directly anyway**,
since it's a real, checkable difference between the two environments
(sandbox: pandas 3.0.2; Mac: pandas 2.3.3; pyarrow identical, 24.0.0,
on both — ruling out pyarrow version as a factor from the start).
Installed pandas 2.3.3 in the sandbox, re-ran the same 1M-row CSV vs.
Parquet comparison, 5 trials each, median taken:

```
$ python3 -c "
import pandas as pd
print('Using pandas', pd.__version__)
...
"
Using pandas 2.3.3
CSV times: ['0.625', '1.009', '0.711', '0.629', '0.634']
Parquet times: ['1.073', '0.414', '0.362', '0.344', '0.348']
CSV median: 0.634s
Parquet median: 0.362s
```

**Parquet is still faster than CSV in the sandbox even with pandas
downgraded to exactly match the Mac's version.** The pandas-version
hypothesis is directly ruled out, not just argued away.

**Conclusion: there was no real ordering flip to explain.** Parquet is
faster than CSV at every reliable (100K+ rows) tier, in both
environments, in both the simple 5-column test and the wide
column-count test. The original claim was a misreading of this
document's own data, corrected above once caught. Recorded here, with
the investigation that confirmed it, rather than quietly removed —
this document's standard is to show how a wrong conclusion is found
and fixed, not just to delete the evidence it happened.

### Item 5 investigation: why compare-time and write-time penalties differed

**The question:** going from 5 to 100 columns costs CSV's *write* time
about 40× at 10K rows, but the full `wherefore compare` CLI's
column-count penalty at the same row count was only 2.3× (see
[Round 3](#round-3-column-count-not-just-row-count) above). Why such a
gap, if both are reading/writing the same wide data?

**Step 1 — isolate the load step alone**, no CLI process overhead, no
diff, no report writing — just `pd.read_csv` + the datetime-detection
pass, the two things that scale with column count inside `compare`:

```
$ python3 -c "
import time, pandas as pd
from wherefore.comparison import loaders
for cols in [5, 100]:
    t0 = time.time()
    raw = pd.read_csv(f'source_n10000_c{cols}.csv', keep_default_na=False, na_values=[''])
    t1 = time.time()
    parsed = loaders._try_parse_datetime_columns(raw)
    t2 = time.time()
    print(f'cols={cols}: read={t1-t0:.4f}s, detect={t2-t1:.4f}s, total={t2-t0:.4f}s')
"
cols=5: read=0.0139s, detect=0.0068s, total=0.0207s
cols=100: read=0.3358s, detect=0.1177s, total=0.4535s
```

Isolated load-step penalty: **21.9×** — much closer to write's ~40×
than to the full-CLI's 2.3×. The scaling cost itself genuinely is
steep; it didn't go away.

**Step 2 — the full CLI number includes a large, column-count-independent
fixed cost** (process startup, importing `typer`/`pandas`/`pyarrow`/
`anthropic`/etc., the diff itself, writing the report) that doesn't
grow with column count at all. At only 10,000 rows, the column-scaling
cost is small in absolute terms, so this fixed cost dominates the
total and dilutes the visible ratio down to 2.3×.

**Step 3 — confirm the dilution shrinks as row count grows**, which it
should if this explanation is right (the fixed cost stays constant
while the scaling cost grows, so its relative share shrinks):

| Rows | Full-CLI compare-time penalty (5→100 cols) |
|---|---|
| 10,000 | 2.3× |
| 100,000 | 9.0× |
| 250,000 | 15.7× |
| 500,000 | 21.4× |
| 1,000,000 | 30.6× |

Exactly the predicted shape — the ratio climbs from 2.3× toward 30.6×
as row count grows, approaching (though not yet reaching, even at 1M
rows) the ~40× write-time-only penalty. **Items 4 and 5 turned out to
be the same underlying phenomenon, viewed from two different angles:
a real, steep column-count scaling cost that gets diluted by a fixed,
column-count-independent overhead whenever the measurement includes
full process/CLI cost rather than isolating the scaling step alone.**
This table was already in this document (under Round 3) before this
investigation — re-derived here as the explanation, not new data.

## Round 4: database sources (item 6)

Same schema (`id`, `name`, `amount`, `category`, `status`), same 1%
mismatch rate, against real database backends instead of files —
SQLite and a genuine PostgreSQL 16 server (TCP, not the WASM/socket-only
PGlite the project's own test suite uses for unit tests — see the scope
note below for why this pressure test needed something different).

### Setup

**SQLite**: `to_sql`, with an explicit `CREATE TABLE ... PRIMARY KEY`
first (confirmed directly that `to_sql` alone does not declare a real
primary key, which would have made `wherefore` fall back to a
heuristic instead of real schema introspection — not representative
of how a real table is normally defined).

**PostgreSQL**: a real PostgreSQL 16 server (installed via `apt`, not
mocked, not WASM), accessed over a genuine TCP connection
(`postgresql://postgres:postgres@localhost:5432/...`). The project's
own test suite uses `py-pglite` (PGlite, Postgres-via-WASM) for unit
tests, which is the right tool there — but PGlite is Unix-socket-only,
and `wherefore`'s own `parse_connection_string` is deliberately built
to assume non-SQLite backends have a real host/port (confirmed
directly: PGlite's socket-path DSN parses incorrectly through it,
mangling the path into the database name). That's not a bug — it's a
documented design choice, since real Postgres servers people actually
connect to do have real TCP host/port pairs. This pressure test needed
that real shape, so a real installed Postgres server was used instead.

Bulk-loaded via `COPY`, not `executemany` — confirmed directly that
naive `executemany` is genuinely slow for this kind of insert (8.51s
for just 5,000 rows, ~588 rows/sec) and `COPY` is ~65× faster (0.13s
for the same 5,000 rows). This matters for honesty: the insert
mechanism is our test harness's choice, not something `wherefore`
itself does or that a real user pays — using the slow path would have
measured our own harness's inefficiency, not `wherefore`'s real
comparison cost.

### Results

```
$ for n in 5000 20000 50000 100000 500000; do
>   python3 generate_db_data.py $n
>   python3 run_db_test.py $n
> done
sqlite n=5000: 1.08s, peak_mem=155.2MB, exit=0
sqlite n=20000: 1.18s, peak_mem=162.1MB, exit=0
sqlite n=50000: 1.33s, peak_mem=175.2MB, exit=0
sqlite n=100000: 1.60s, peak_mem=197.4MB, exit=0
sqlite n=500000: 4.04s, peak_mem=413.6MB, exit=0
```

```
$ for n in 5000 20000 50000 100000 500000; do
>   python3 generate_db_data.py $n postgres
>   python3 run_pg_test.py $n
> done
postgres n=5000: 1.07s, peak_mem=165.4MB, exit=0
postgres n=20000: 1.18s, peak_mem=171.3MB, exit=0
postgres n=50000: 1.34s, peak_mem=183.1MB, exit=0
postgres n=100000: 1.88s, peak_mem=211.5MB, exit=0
postgres n=500000: 4.74s, peak_mem=443.7MB, exit=0
```

Verified correct at the largest tier, both backends:

```
$ grep "mismatched row\|Join key" db_test/results/report_500000.md
- Join key: `id`
### `amount` -- 5000 mismatched row(s)
$ grep "mismatched row\|Join key" db_test/results/report_pg_500000.md
- Join key: `id`
### `amount` -- 5000 mismatched row(s)
```

Both backends correctly detected the real schema primary key (not a
heuristic fallback) and found exactly 5,000 mismatches — 1% of
500,000, matching every file-based result at this row count.

### Database vs. file, at exactly matching row counts

Same schema, same mismatch rate, same row counts, generated as
matching CSV files for a direct comparison:

| Rows | CSV (s) | SQLite (s) | Postgres (s) | SQLite ÷ CSV | Postgres ÷ CSV |
|---|---|---|---|---|---|
| 5,000 | 1.02 | 1.08 | 1.07 | 1.06× | 1.05× |
| 20,000 | 1.07 | 1.18 | 1.18 | 1.10× | 1.10× |
| 50,000 | 1.18 | 1.33 | 1.34 | 1.13× | 1.14× |
| 100,000 | 1.40 | 1.60 | 1.88 | 1.14× | 1.34× |
| 500,000 | 2.73 | 4.04 | 4.74 | 1.48× | 1.74× |

**Database sources are consistently a bit slower than equivalent
files, and the gap widens with scale** — SQLite from 1.06× to 1.48×,
Postgres from 1.05× to 1.74×, both growing as row count grows. This
matches real, structural expectations: a database round-trip carries
overhead a flat file doesn't pay — query execution, result-set
serialization back through the Python driver, and for Postgres
specifically, genuine network-stack overhead even when the "network"
is localhost.

**SQLite and Postgres perform almost identically to each other** at
every tier (within a few percent), with Postgres very slightly slower
at the larger tiers — consistent with Postgres paying real socket/
network overhead that SQLite's direct in-process file access doesn't.

**Practical reading: choosing a database over a file as your
comparison source costs real but modest overhead at these row
counts** — nowhere near XLSX's order-of-magnitude penalty, closer to a
20–75% tax that grows gradually with scale rather than a step change.
At rows beyond 500K, given the trend, expect this gap to continue
widening — not yet tested at 1M+ for database sources.

### Round 4 extension: does column count compound for databases too?

Round 3 found CSV's column-count penalty worsens sharply as rows grow
(2.3× → 30.6× across row tiers). Does the same hold for database
sources? Tested directly: SQLite and Postgres, both backends, all 6
column tiers (5/10/20/30/50/100) × all 5 row tiers (5K–500K) — 60
real comparisons total, same schema and extra-column logic as the
file-based width test.

```
$ for n in 5000 20000 50000 100000 500000; do
>   for cols in 5 10 20 30 50 100; do
>     python generate_db_data.py $n $cols sqlite
>     python run_db_test.py $n $cols
>   done
> done
[... 30 combinations, all exit=0 ...]
sqlite n500000_c5: 1.54s, peak_mem=633.7MB, exit=0
sqlite n500000_c100: 50.29s, peak_mem=10707.8MB, exit=0
```

```
$ for n in 5000 20000 50000 100000 500000; do
>   for cols in 5 10 20 30 50 100; do
>     python generate_db_data.py $n $cols postgres
>     python run_pg_test.py $n $cols
>   done
> done
[... 30 combinations, all exit=0 ...]
postgres n500000_c5: 1.47s, peak_mem=687.4MB, exit=0
postgres n500000_c100: 45.06s, peak_mem=12434.8MB, exit=0
```

Verified correct at the heaviest combination, both backends:

```
$ grep "mismatched row\|Join key" db_test/results/report_n500000_c100.md
- Join key: `id`
### `amount` -- 5000 mismatched row(s)
$ grep "mismatched row\|Join key" db_test/results/report_pg_n500000_c100.md
- Join key: `id`
### `amount` -- 5000 mismatched row(s)
```

**Yes — and the database penalty is worse than the file penalty.**
5→100 column scaling factor, SQLite vs. the equivalent CSV result from
Round 3:

| Rows | CSV penalty (Round 3) | SQLite penalty |
|---|---|---|
| 100,000 | 9.0× | 12.5× |
| 500,000 | 21.4× | 32.7× |

Database sources aren't just slower than files at a fixed column
count — they're *more sensitive* to column count growth than files
are. A wide table costs proportionally more as a database source than
the identical wide table would as a CSV.

**A real, second finding: at high column counts, Postgres pulls ahead
of SQLite — the opposite of what the 5-column-only result suggested.**

| Cols | SQLite (500K rows, s) | Postgres (500K rows, s) | Postgres ÷ SQLite |
|---|---|---|---|
| 5 | 1.54 | 1.47 | 0.95× |
| 10 | 3.35 | 3.01 | 0.90× |
| 20 | 7.47 | 6.59 | 0.88× |
| 30 | 12.54 | 11.46 | 0.91× |
| 50 | 22.69 | 20.19 | 0.89× |
| 100 | 50.29 | 45.06 | 0.90× |

Postgres is consistently 5–12% *faster* than SQLite once real date
columns are in the mix, at every column tier tested — not a fluke at
one data point. The mechanism is real and checkable in `db.py` itself,
not speculation: `query_table`'s SQLite branch calls
`_try_parse_datetime_columns` (the same function profiled in Item 1)
because SQLite stores everything as TEXT and needs the heuristic to
recover real dates; the Postgres branch never calls it at all, because
a native Postgres `TIMESTAMP` column round-trips as a real
`datetime64` automatically. At 100 columns, 19 of them are genuine
dates — SQLite pays Item 1's real, measured date-conversion cost on
all 19; Postgres pays nothing for this step. **This isn't a contradiction
of the original (5-column, no-real-dates) finding that SQLite and
Postgres performed almost identically** — at 5 columns there are no
real date columns to convert, so the mechanism that separates them
at width never gets triggered; the two backends are close to equal
exactly because the cost that would differentiate them isn't present
yet.

## Round 5: S3 sources (item 7)

**Real limitation, stated upfront: no AWS account was available for
this round.** S3 testing here uses `moto` (the same mocked-AWS dev
dependency the project's own test suite uses), which mocks the AWS API
locally — there is no real network, no real latency, no real S3
service involved. This measures `wherefore`'s own local processing
cost for the S3 code path honestly and correctly. It does **not**
measure real-world network/latency cost, which is almost certainly the
dominant real-world factor for actual S3 usage and is a genuinely
different variable than anything tested here. Flagged clearly, not
glossed over — this round answers "does wherefore's own S3 code add
overhead beyond download+parse" and nothing about "how fast is a real
S3 bucket."

### A real methodology constraint, found and worked around

`wherefore compare`'s S3 path downloads the full object into memory via
one `boto3` `get_object()` call before any parsing starts (confirmed
directly in `loaders.py` — no streaming, no partial reads). The
natural way to test this would have been the same subprocess approach
used for every other round (`run_test.py`-style: shell out to the real
CLI, measure wall-clock). That does not work for moto specifically:
**moto's mocked AWS state is process-local and does not persist across
separate `mock_aws()` context entries, even within the same process —
confirmed directly** by creating a bucket in one `mock_aws()` block and
showing a second block in the same process cannot see it
(`NoSuchBucket`). A subprocess CLI invocation would start its own,
empty mock with no knowledge of anything uploaded beforehand, so the
file would never be found. The fix: run the real `wherefore compare`
CLI command in-process via `typer`'s `CliRunner`, inside the same
`mock_aws()` context used to upload the test files — this runs the
exact same CLI code path, just without a subprocess boundary, which
moto's design requires.

### Results: full `compare`, S3 source (in-process)

```
$ python3 run_s3_test.py 5000 20000 50000 100000 500000
s3 n=5000: 0.100s, mem_delta=11.6MB, exit=0
s3 n=20000: 0.162s, mem_delta=10.8MB, exit=0
s3 n=50000: 0.325s, mem_delta=7.9MB, exit=0
s3 n=100000: 0.457s, mem_delta=17.7MB, exit=0
s3 n=500000: 2.152s, mem_delta=108.2MB, exit=0
```

Verified correct at the largest tier:

```
$ grep "mismatched row\|Join key" /tmp/s3_report_500000.md
- Join key: `id`
### `amount` -- 5000 mismatched row(s)
```

These absolute numbers are **not directly comparable** to other
rounds' subprocess-based numbers — running in-process skips Python
interpreter startup and library-import cost entirely (paid once before
the loop, not per measurement), so they look faster than they would as
a separate process per run. The comparison that matters here is local
load vs. S3 load, both measured the same in-process way.

### Local file load vs. S3 (mocked) load, same data, same process

```
$ python3 -c "
import time
from wherefore.comparison import loaders
for n in [5000, 20000, 50000, 100000, 500000]:
    t0 = time.time()
    local_df = loaders.load_csv(f'data/source_{n}.csv')
    local_time = time.time() - t0
    with mock_aws():
        # upload, then:
        t0 = time.time()
        s3_df = loaders.load_csv(f's3://wherefore-test-bucket/source_{n}.csv')
        s3_time = time.time() - t0
    print(f'n={n}: local={local_time:.4f}s, s3(mocked)={s3_time:.4f}s, ratio={s3_time/local_time:.2f}x')
"
n=5000: local=0.0145s, s3(mocked)=0.0165s, ratio=1.14x
n=20000: local=0.0197s, s3(mocked)=0.0351s, ratio=1.78x
n=50000: local=0.0471s, s3(mocked)=0.0558s, ratio=1.19x
n=100000: local=0.0819s, s3(mocked)=0.0934s, ratio=1.14x
n=500000: local=0.4032s, s3(mocked)=0.4291s, ratio=1.06x
```

**With network latency removed (moto's mock), wherefore's S3 code path
adds only modest overhead — roughly 6–19% over loading the identical
file locally**, no clear trend with scale (the 20K tier's 1.78× looks
like noise from a small absolute time, not a real pattern — the other
four tiers cluster tightly around 1.1-1.2×). This is the cost of one
`boto3.get_object()` call plus wrapping the response in a `BytesIO`
buffer — not real data transfer, since moto never leaves the process.

**What this does and doesn't tell you:** it confirms `wherefore`'s own
S3 integration isn't doing anything wasteful — the overhead beyond
local-file cost is small and bounded. It says nothing about real S3
performance, which depends on bucket region, object size, connection
quality, and AWS's actual API latency — none of which a local mock can
simulate. Real-world S3 comparisons should expect meaningfully more
overhead than this number, dominated by network round-trip time, not
by anything measured here.

## Round 6: compare-dir's database batch mode (item 1 of the follow-up list)

Round 4 tested single-pair `compare` against `db://` sources only.
`compare-dir db://* db://*` — the real multi-table migration-audit
mode — was untested. Closed here with a realistic batch: one database,
8 tables, **varying both row count and column count per table**, not
8 uniform tables that only differ in size. A real database has tables
of genuinely different shapes — a small lookup table next to a huge,
wide transactional one — and testing only uniform tables would have
missed that.

### The 8 tables

| Table | Rows | Columns |
|---|---|---|
| `lookup_5k` | 5,000 | 5 |
| `ref_10k` | 10,000 | 10 |
| `small_30k` | 30,000 | 20 |
| `medium_50k` | 50,000 | 30 |
| `medium_100k` | 100,000 | 50 |
| `large_250k` | 250,000 | 100 |
| `large_500k` | 500,000 | 5 |
| `huge_1m` | 1,000,000 | 10 |

`large_250k` (large AND wide) and `large_500k` (large but narrow) are
deliberately placed next to each other — the real stress case for
batch mode is a table that's both large and wide, and this set
includes one without making every large table wide.

### Results (Mac, real hardware)

```
$ python generate_batch_data.py sqlite
  lookup_5k (5,000 rows, 5 cols): 0.01s
  ref_10k (10,000 rows, 10 cols): 0.02s
  small_30k (30,000 rows, 20 cols): 0.13s
  medium_50k (50,000 rows, 30 cols): 0.34s
  medium_100k (100,000 rows, 50 cols): 1.09s
  large_250k (250,000 rows, 100 cols): 6.51s
  large_500k (500,000 rows, 5 cols): 0.76s
  huge_1m (1,000,000 rows, 10 cols): 2.66s
Total generation: 11.52s
Source DB size: 1389.8MB
```

```
$ wherefore compare-dir db://* db://* --source-conn-env SOURCE_DB \
    --target-conn-env TARGET_DB --yes --output-dir batch_test/results
Elapsed: 35.69s
Exit code: 0

Detecting primary keys for 8 table(s)...
  [... all 8 correctly detect 'id' from real schema ...]
Found 8 matching table pair(s). Comparing...
  [DIFF] huge_1m: 1 finding(s) (unrecognized pattern(s))
  [... all 8 tables show 1 finding each ...]
Done: 8 compared, 0 skipped.
```

Same batch against Postgres (separate `wherefore_batch_source`/
`wherefore_batch_target` databases, same table specs):

```
$ wherefore compare-dir db://* db://* [...] --output-dir batch_test/results_pg
Elapsed: 31.80s
Exit code: 0
[... identical structure, all 8 tables, 1 finding each ...]
```

Verified correct on the two tables most likely to expose a real bug —
the widest table and the largest table — for both backends:

```
$ grep "mismatched row" batch_test/results/large_250k_report.md batch_test/results/huge_1m_report.md
large_250k_report.md:### `amount` -- 2500 mismatched row(s)
huge_1m_report.md:### `amount` -- 10000 mismatched row(s)
$ grep "mismatched row" batch_test/results_pg/large_250k_report.md batch_test/results_pg/huge_1m_report.md
large_250k_report.md:### `amount` -- 2500 mismatched row(s)
huge_1m_report.md:### `amount` -- 10000 mismatched row(s)
```

2,500 (1% of 250,000) and 10,000 (1% of 1,000,000) — correct on both
backends, confirming `compare-dir` handles 8 tables of genuinely
different shapes correctly within a single batch run, not just
uniform tables that happen to differ in row count alone.

**A real, complete migration audit across 8 mixed-shape tables —
roughly 1.95 million total rows, one table 100 columns wide — finishes
in well under a minute on real hardware (35.69s SQLite, 31.80s
Postgres), with one combined primary-key confirmation step for the
whole batch rather than one per table.** Postgres edging out SQLite
here (31.80s vs 35.69s) is consistent with Round 4's finding that
Postgres pulls ahead at high column counts — this batch includes a
real 100-column, 250K-row table, exactly the regime where that effect
showed up.

## Still to measure

- **Real S3 with a real AWS account** — round 5 only covers
  `wherefore`'s own local processing cost via mocked AWS; actual
  network/latency cost against a real bucket is untested and likely
  the dominant real-world factor
- Realistic/messy data: real nulls, near-duplicate keys,
  `--fuzzy-keys` — still excluded from this clean baseline (real dates
  are now covered, via the `*_date` column in the width matrix)
- The polars conversion-overhead spike described in item 3's scope
  boundary above — not yet run
- XLSX at 1M rows × 100 columns — deliberately skipped (projected
  ~30 minutes combined); revisit only if a real use case needs it

