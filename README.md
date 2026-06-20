# wherefore

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

🚧 **Actively built in public. The CLI works end-to-end through statistical pattern detection — the AI narrative layer is the next piece.**

What's real today:
- **A working CLI**: `wherefore compare a.csv b.csv` runs against real
  files on disk and produces a report — see [Try it yourself](#try-it-yourself-with-your-own-files)
  below
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
- Three fully implemented, end-to-end-tested failure patterns:
  `timezone_shift`, `truncation`, and `enum_drift` — corruptor →
  detection signature → registry → real diff → real cluster match →
  real CLI report, each proven against real files. Tested together
  against the same dataset to confirm clustering distinguishes all
  three correctly with zero cross-contamination, including a real
  false-positive bug caught and fixed between `truncation` and
  `enum_drift` (both target string columns) — see
  [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md)
- The AI reasoning layer is built and **verified against the real
  Claude API**: a `ClusterExplanation` schema, a swappable `Provider`
  interface, and a real Claude integration that uses *forced* tool-use
  so the model can't return free-text prose — it must call a tool
  whose schema is generated directly from the pydantic model, so the
  two can't silently drift apart. Tested against real fixtures from
  all three patterns plus a genuinely unrecognized case — across all
  four, the model gave sound causal reasoning, correctly ruled out
  competing explanations using the actual data, and correctly refused
  to force-fit a pattern when none applied. See
  [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the full results.
- **Wired into the CLI** behind an explicit `--explain` flag — off by
  default, so the tool stays free and key-free for anyone just trying
  it out. With `--explain` (and `ANTHROPIC_API_KEY` set), the report
  shows the AI's narrative *alongside* the statistical evidence it was
  reasoned from, not instead of it — so you can verify the claim
  against the raw data yourself.

What's not built yet: the eval harness scoring loop that runs this at
scale against many labeled fixtures and computes precision/recall per
corruption type. See [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the
live build queue.

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
source.csv, target.csv
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
 ── wherefore compare stops here today, reporting statistical matches ──
        │
        ▼  (not built yet)
 AI reasoning layer        (Claude, behind a swappable explain() interface;
        │                   takes statistically-flagged clusters, writes
        │                   the causal narrative, cites real example rows,
        │                   honestly flags "unrecognized" when nothing fits)
        ▼
 Markdown report
```

**Failure patterns are data, not code.** Each known failure mode
(timezone shift, truncation, encoding mismatch, null/type coercion,
dedup failure, key mismatch, float precision loss, enum drift) is a YAML
file under `src/wherefore/taxonomy/patterns/`, validated against a strict
schema. Adding a new pattern means writing a YAML file and a small
corruptor function — never touching clustering or reasoning code. See
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
free, no API key), against all 4 committed fixtures:**

```
$ python3 -m evals.harness.run_eval
=== Statistical eval (clustering only, free, no API key) ===
Total cases: 4
Overall accuracy (correct match + honest abstain): 100.00%
Outcome breakdown: {'true_positive': 3, 'honest_abstain': 1}

  enum_drift: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
  timezone_shift: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
  truncation: precision=1.00 recall=1.00 (TP=1 FP=0 FN=0)
```

This is reproducible by anyone who clones the repo — run the command
above yourself. Fixtures and their ground-truth labels are committed
(`evals/fixtures/`), generated by `evals/fixtures/regenerate.py` using
the real corruptor functions, not hand-written or invented.

**LLM mode** (`--llm` flag, real Claude API calls, scores `explain()`'s
narrative attribution instead of clustering's raw statistics) exists
and is gated the same way `--explain` is in the CLI — requires
`ANTHROPIC_API_KEY`, off by default. Run it yourself:
`python3 -m evals.harness.run_eval --llm`.

Four fixtures is a small sample — enough to prove the harness itself
works correctly, not enough to claim robust statistical confidence.
Expanding fixture coverage (more examples per pattern, edge cases,
multi-corruption fixtures) is tracked in
[`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).

## Getting started

```bash
git clone https://github.com/ArunMishra1/wherefore.git
cd wherefore
./dev_setup.sh
```

This creates a `.venv/`, installs the package in editable mode with dev
dependencies, and runs the test suite (should show **142 passed**). It's
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

That's the whole interface. Two files in, a Markdown report out. No
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

If nothing in the taxonomy matches what's actually wrong in your data,
`wherefore` says so — `pattern unrecognized` — rather than forcing a
guess, and (with `--explain`) the AI does the same: in testing, it
correctly identified a genuinely random, non-matching corruption and
proposed real alternative hypotheses (a bad join, a mis-wired column)
instead of inventing a pattern that wasn't there. Right now the
taxonomy has three patterns (`timezone_shift`, `truncation`,
`enum_drift`); more are being added, tracked in
[`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).

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

