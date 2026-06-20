# wherefore

[![CI](https://github.com/ArunMishra1/wherefore/actions/workflows/ci.yml/badge.svg)](https://github.com/ArunMishra1/wherefore/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

**Explains *why* two datasets differ — not just that they do.**

Data diffing tools (data-diff, Great Expectations, OpenMetadata data diff,
datacompy) tell you *that* 40 rows mismatched in column `created_at`.
None of them tell you *why* — that those 40 mismatches share one root
cause: a timezone conversion applied inconsistently during a migration
window. `wherefore` is the layer that sits on top of a diff and answers
that question, in plain English, with real example rows cited, and
honestly says "I don't recognize this pattern" when nothing fits known
failure modes — rather than confidently guessing wrong.

This is not a thin prompt wrapper. The AI reasoning layer sits behind a
deterministic clustering and statistical-signature step, and every
accuracy claim this project makes is backed by an eval harness scored
against labeled synthetic ground truth — see [Evals](#evals--why-trust-the-explanations) below.

---

## Status

🚧 **Actively built in public. The full pipeline works end-to-end — statistical detection, AI explanation, and a scored eval harness all real and verified.**

What's real today:
- **A working CLI**: `wherefore compare a.csv b.csv` runs against real
  files on disk and produces a report — see [Try it yourself](#try-it-yourself-with-your-own-files)
  below
- **Batch mode**: `wherefore compare-dir source/ target/` compares
  every matching filename across two directories in one run — the
  real shape of a migration audit (dozens of tables, not one). One
  report per pair, a failure on any single pair is skipped and
  reported, not fatal to the whole batch.
- **CSV, JSON, Parquet, and Excel (.xlsx/.xls)** all supported as
  input formats, auto-detected by file extension. Parquet specifically
  sidesteps a whole class of CSV round-trip issues this project hit
  twice (datetime precision, null-sentinel-blocks-parsing) since it's
  natively typed — see [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for
  the real bugs this surfaced and a documented, honest limitation
  (Parquet's strong typing means a column can't always represent a
  mixed-type null-coercion bug the way CSV/Excel can)
- Comparison engine wrapping `datacompy`: schema-aware diffing,
  composite join keys, dtype-mismatch detection distinct from
  value-mismatch detection
- Fuzzy key matching for when source/target keys don't align exactly
  (e.g. a key column reformatted during migration), with deliberate
  safeguards against false-confidence matches and ambiguous ties
- Deterministic clustering: groups mismatches by column, runs
  statistical signature checks against candidate taxonomy patterns,
  outputs confidence-scored matches with **zero causal language** —
  enforced by a structural test, not just convention
- The taxonomy system: failure patterns are defined as data (YAML), not
  code, validated against a strict schema — see [Architecture](#architecture)
- Five fully implemented, end-to-end-tested failure patterns:
  `timezone_shift`, `truncation`, `enum_drift`, `null_type_coercion`,
  and `float_precision` — corruptor → detection signature → registry →
  real diff → real cluster match → real CLI report, each proven
  against real files. Building the fourth pattern surfaced three real
  bugs spanning the comparison engine, the loaders, and the eval
  harness itself, and the fifth surfaced a subtler lesson — a
  magnitude-based heuristic that looked right scored a real false
  positive on an adversarial test case, fixed by checking the
  underlying mechanism (an exact float32 round-trip) directly instead
  of approximating its size — see
  [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the full account of
  both.
- **Data sent to Claude is redacted by default.** Before any cell
  value reaches `--explain`'s prompt, it passes through a pattern-based
  redaction layer that masks emails, SSNs, credit card numbers, and
  phone numbers — secure-by-default, not opt-in, since opt-in
  redaction tends to mean most people never enable it. Disable with
  `--no-redact` if you've already vetted your data. This is honestly
  scoped: it catches *structurally recognizable* patterns, not a
  general PII detector — see [`SECURITY.md`](./SECURITY.md) for exactly
  what it does and doesn't catch, including a documented false-positive
  case (long numeric IDs can look like credit card numbers) found
  during testing.
- The AI reasoning layer is built and **verified against the real
  Claude API, twice**: once by hand against four cases, and once via
  the scored eval harness across all five patterns plus the
  unrecognized case — 100% accuracy both times (small sample, see
  [Evals](#evals--why-trust-the-explanations) for the honest caveat).
  Uses *forced* tool-use so the model can't return free-text prose —
  it must call a tool whose schema is generated directly from the
  pydantic model, so the two can't silently drift apart. In testing,
  it gave sound causal reasoning, correctly ruled out competing
  explanations using the actual data, and correctly disambiguated a
  cluster where clustering itself reported two legitimate candidates —
  see [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the full account.
- **Wired into the CLI** behind an explicit `--explain` flag — off by
  default, so the tool stays free and key-free for anyone just trying
  it out. With `--explain` (and `ANTHROPIC_API_KEY` set), the report
  shows the AI's narrative *alongside* the statistical evidence it was
  reasoned from, not instead of it — so you can verify the claim
  against the raw data yourself.

What's next, not yet done: expanding fixture coverage so the eval
numbers carry more statistical weight (six fixtures proves the
mechanism works, not that it's bulletproof at scale), a sixth taxonomy
pattern (`encoding_mismatch`), and database/cloud-storage connectivity
(currently file-based only: CSV, JSON, Parquet, Excel). See
[`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the live build queue.

## The problem, in plain terms

Imagine two boxes of identical LEGO sets. Someone copied box A into box
B, but a few pieces are missing or the wrong color. Most tools that
check this say: *"12 pieces are different."* That's it.

`wherefore` looks at those 12 differences and says: *"These aren't
random — every one of them has the same color swapped the same way,
consistent with a colorblind sort. That's your root cause."* It explains
the pattern behind the differences, not just the differences themselves.

To know if the tool is actually doing this well (not just sounding
plausible), we build our own "messed-up" datasets on purpose — corrupt
them in a specific, *labeled* way — and grade whether the tool correctly
identifies what we did. That's the eval harness, and it's first-class
in this project, not an afterthought.

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
 normalized diff result
        │
        ▼
 deterministic clustering  (groups mismatches; runs cheap statistical
        │                   signature checks — NO causal claims here)
        ▼
 candidate pattern(s), confidence-scored
        │
        ├─── default: stop here, report statistics only (free, no API key)
        │
        ▼  with --explain
 AI reasoning layer        (Claude, behind a swappable explain() interface;
        │                   redacts common sensitive patterns by default,
        │                   takes statistically-flagged clusters, writes
        │                   the causal narrative, cites real example rows,
        │                   honestly flags "unrecognized" when nothing fits)
        ▼
 Markdown report           (statistics always; AI narrative alongside
                             the evidence when --explain is used)
```

**Failure patterns are data, not code.** Each known failure mode is a
YAML file under `src/wherefore/taxonomy/patterns/`, validated against
a strict schema. Five are built and tested today — `timezone_shift`,
`truncation`, `enum_drift`, `null_type_coercion`, `float_precision` —
with more planned (`encoding_mismatch`, `key_mismatch`,
`dedup_failure`; see [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md)). Adding
a new pattern means writing a YAML file and a small corruptor function
— never touching clustering or reasoning code. See
[`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full contract and the
design tradeoffs behind it.

**Clustering and reasoning are deliberately separated.** The clustering
layer only ever produces statistical observations ("these 12 rows differ
by exactly 5 hours"). Causal attribution ("this is a timezone bug") is
the LLM's job, every time — if clustering started asserting causes, the
AI layer would become decorative and the eval harness would stop
measuring anything meaningful.

## Evals — why trust the explanations?

Because we control the ground truth. The synthetic data generator
creates clean datasets, then deliberately corrupts them using one of
the taxonomy's known failure patterns — and records exactly what it
did and to which rows in a committed `ground_truth.json`. The eval
harness runs the real pipeline against these labeled fixtures and
scores the result, tracked as precision/recall per pattern, with
"correctly said unrecognized" tracked separately from "confidently
named the wrong pattern" — those are very different failure modes a
naive right/wrong scorer would conflate.

**Current results, statistical mode (clustering's signature match,
free, no API key), against all 6 committed fixtures:**

```
$ python3 -m evals.harness.run_eval
=== Statistical eval (clustering only, free, no API key) ===
Total cases: 6
Overall accuracy (correct match + honest abstain): 100.00%
Outcome breakdown: {'true_positive': 5, 'honest_abstain': 1}

  enum_drift: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
  float_precision: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
  null_type_coercion: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
  timezone_shift: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
  truncation: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
```

This is reproducible by anyone who clones the repo — run the command
above yourself. Fixtures and their ground-truth labels are committed
(`evals/fixtures/`), generated by `evals/fixtures/regenerate.py` using
the real corruptor functions, not hand-written or invented.

One real subtlety worth knowing: `null_type_coercion` and `enum_drift`
can legitimately BOTH match the same cluster (a null consistently
coerced to one sentinel string is, statistically, also a "consistent
value mapping"). Clustering reports both honestly rather than guessing
which is "more right" — that's a causal judgment, and causal judgment
is the AI layer's job, not clustering's. The eval harness scores this
correctly too: a true pattern counts as found if it appears anywhere
among the reported candidates, not only if it happens to be listed
first. See [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the full story
of how this was discovered and fixed.

**LLM mode** (`--llm` flag, real Claude API calls, scores `explain()`'s
narrative attribution instead of clustering's raw statistics) exists
and is gated the same way `--explain` is in the CLI — requires
`ANTHROPIC_API_KEY`, off by default. Run it yourself:
`python3 -m evals.harness.run_eval --llm`.

**Run for real against all 6 fixtures: 100% accuracy** — every
`matched_pattern_id` the AI committed to matched ground truth,
including the one fixture specifically designed to test something the
statistical layer alone can't: `fixture_null_type_coercion_001`'s
cluster legitimately scores two correct statistical candidates
(`null_type_coercion` and `enum_drift` — see the dual-match note
above), and `explain()` is forced to commit to exactly one via its
tool schema. It picked correctly — not by defaulting to whichever
candidate clustering listed first, but by reasoning that a genuinely
null source becoming the literal text "NULL" points to type coercion,
not a deliberate value rename. That's the real test of whether letting
the AI disambiguate (rather than hardcoding a priority rule into
clustering) was the right call, and on this sample, it was.

Six fixtures is a small sample — enough to prove both the statistical
layer and the AI layer work correctly on a real, reproducible run, not
enough to claim robust statistical confidence at scale. Expanding
fixture coverage (more examples per pattern, edge cases,
multi-corruption fixtures) is tracked in
[`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).

## Getting started

```bash
git clone https://github.com/ArunMishra1/wherefore.git
cd wherefore
./dev_setup.sh
```

This creates a `.venv/`, installs the package in editable mode with dev
dependencies, and runs the test suite (should show **230 passed**). It's
safe to re-run — it skips recreating an existing `.venv`.

**No API key needed for this.** The test suite covers the AI reasoning
layer entirely with a fake provider — no network calls, no cost. An
`ANTHROPIC_API_KEY` is only needed if you want to actually run
`explain()` against the real Claude API:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

After the first run, activate the environment in new shells with:

```bash
source .venv/bin/activate
```

<details>
<summary>Manual setup (if you'd rather not run the script)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e ".[dev]"
pytest tests/ -v
```
</details>

**Requires Python 3.10+.** Tested on 3.10–3.12. If you're on a very
recent Python (e.g. 3.14), pandas/numpy themselves are compatible, but
if `pip install` fails, a smaller transitive dependency without 3.14
wheels yet is the likely cause — try a 3.11/3.12 interpreter if you hit
this.

### Try it yourself, with your own files

```bash
wherefore compare old_export.csv new_export.csv --output report.md
```

That's the whole interface. Two files in, a Markdown report out. Works
the same way with `.json`, `.parquet`, or `.xlsx`/`.xls` — mix and
match formats freely (e.g. compare a `.csv` export against a
`.parquet` file) since the format is auto-detected per file. No
key column required — `wherefore` looks at your columns and picks the
one that looks like an identifier (mostly-unique values, often named
something with "id" or "key" in it). If it picks wrong, or your files
don't share an obvious key, tell it directly:

```bash
wherefore compare old_export.csv new_export.csv --key employee_id
```

If the same record has a different-looking key on each side — a
common symptom of a migration where IDs got reformatted, e.g.
`EMP-1001` became `EMP1001` — add `--fuzzy-keys`:

```bash
wherefore compare old_export.csv new_export.csv --fuzzy-keys
```

Here's a concrete run. Two small HR exports, identical except every
`hire_date` is five hours later in the new file — the kind of thing
that happens when an export job's server timezone changes during a
migration and nobody notices until payroll runs wrong:

```bash
$ wherefore compare old_export.csv new_export.csv --output report.md
Compared 5 source rows against 5 target rows.
Matched rows: 5
  hire_date: 5 mismatches, matches 'timezone_shift' (confidence 1.00)

Full report written to report.md
```

That confidence score is a real, deterministic measurement — every
mismatched value differs from its source by exactly the same 5-hour
delta, which is the statistical signature `wherefore` checks for. By
default that's *all* you get — real diffing, real grouping, real
pattern matching, with no AI involved and no API key needed.

To get an actual plain-English explanation of *why* this happened —
not just confirmation that the pattern matched — add `--explain`
(requires `ANTHROPIC_API_KEY`; this makes a real, billed API call per
cluster):

```bash
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

The report includes the AI's narrative *alongside* the statistical
evidence it was reasoned from — not instead of it — so you can check
the claim against the actual data yourself rather than trusting it
blindly.

**Before any value reaches that API call, it's checked against a
redaction pattern** for emails, SSNs, credit card numbers, and phone
numbers — on by default, no flag needed. If anything gets masked,
you'll see it called out (`Redacted before sending to Claude: email`).
This is pattern-based, not a general PII scanner — see
[`SECURITY.md`](./SECURITY.md) for exactly what it does and doesn't
catch. Use `--no-redact` if you've already vetted your data and want
raw values in the prompt.

If nothing in the taxonomy matches what's actually wrong in your data,
`wherefore` says so — `pattern unrecognized` — rather than forcing a
guess, and (with `--explain`) the AI does the same: in testing, it
correctly identified a genuinely random, non-matching corruption and
proposed real alternative hypotheses (a bad join, a mis-wired column)
instead of inventing a pattern that wasn't there. Right now the
taxonomy has five patterns (`timezone_shift`, `truncation`,
`enum_drift`, `null_type_coercion`, `float_precision`); more are being
added, tracked in [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).

### Comparing a whole migration, not one table

Real migrations are dozens of tables, not one. `compare-dir` runs the
same comparison across every matching filename between two
directories — `old_exports/accounts.csv` pairs with
`new_exports/accounts.csv`, and so on:

```bash
$ wherefore compare-dir old_exports new_exports --output-dir reports
Found 3 matching file pair(s). Comparing...

  [DIFF] accounts.csv: 1 column(s) affected (timezone_shift)
  [DIFF] patients.csv: 1 column(s) affected (truncation)
  [OK] transactions.csv: no mismatches

Done: 3 compared, 0 skipped. Reports written to reports/
```

One report per pair lands in `reports/`. Files are matched by
identical filename only — no fuzzy matching at the file level, since
guessing wrong about *which two tables* you're comparing is a much
worse mistake than guessing wrong about a row key (which already has
its own careful, opt-in `--fuzzy-keys`). A file that can't be compared
(bad format, no detectable key) is skipped and reported — it doesn't
abort the rest of the batch. Every flag from `compare` works here too
(`--key`, `--fuzzy-keys`, `--explain`, `--no-redact`), applied to every
pair in the run.

<details>
<summary>All flags</summary>

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

wherefore compare-dir SOURCE_DIR TARGET_DIR [OPTIONS]

  --output-dir TEXT             Directory for one report per pair (default: reports).
  --key, --fuzzy-keys, --confidence-threshold, --explain, --no-redact   Same as `compare`, applied to every pair.
```
</details>

## Contributing

Contributions are welcome, especially new taxonomy patterns. Start with
[`CONTRIBUTING.md`](./CONTRIBUTING.md) — it covers the pattern contract,
why patterns are built corruptor-first rather than YAML-first, and the
design decisions worth knowing before you dig in (single-signature
detection hints, why eval fixtures are committed, why clustering never
makes causal claims).

Found a security issue? See [`SECURITY.md`](./SECURITY.md).

## License

Apache License 2.0 — see [`LICENSE`](./LICENSE). Contributions are
accepted under the same license (see `NOTICE` for attribution).

