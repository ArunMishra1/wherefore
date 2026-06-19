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
- One fully implemented, end-to-end-tested failure pattern:
  `timezone_shift` — corruptor → detection signature → registry → real
  diff → real cluster match → real CLI report, all proven against
  real files in both synthetic domains

What's not built yet: the AI reasoning layer that turns a statistical
match into a plain-English causal explanation, and the eval harness
scoring loop. Until the reasoning layer exists, the CLI report shows
*what* was statistically detected, not *why* it happened — the report
says this explicitly so nobody mistakes a confidence score for an
explanation. See [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the live
build queue.

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
creates clean datasets, then deliberately corrupts them using one of the
taxonomy's known failure patterns — and records exactly what it did and
to which rows. The eval harness runs the full pipeline against these
labeled fixtures and scores whether the AI's root-cause explanation
matches the actual injected cause, tracked as precision/recall per
corruption type. Fixtures and their ground-truth labels are committed to
the repo (`evals/fixtures/`) so anyone can reproduce the numbers exactly.

This is in progress — see [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) and
the `evals/` directory for current state. Accuracy numbers will be added
here once the harness is running for real.

## Getting started

```bash
git clone https://github.com/ArunMishra1/wherefore.git
cd wherefore
./dev_setup.sh
```

This creates a `.venv/`, installs the package in editable mode with dev
dependencies, and runs the test suite (should show **82 passed**). It's
safe to re-run — it skips recreating an existing `.venv`.

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
delta, which is the statistical signature `wherefore` checks for. It
is **not** an AI saying "this looks like a timezone bug" — that
narrative layer doesn't exist yet (see [Status](#status)). What you get
today is the honest middle step: real diffing, real grouping, real
pattern matching against a known taxonomy, with the gap to "and here's
why" stated plainly in the report itself.

If nothing in the taxonomy matches what's actually wrong in your data,
`wherefore` says so — `pattern unrecognized` — rather than forcing a
guess. Right now the taxonomy has one pattern (`timezone_shift`); more
are being added, tracked in [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).

<details>
<summary>All flags</summary>

```bash
wherefore compare SOURCE TARGET [OPTIONS]

  --key TEXT                   Join key column. Auto-detected if omitted.
  --fuzzy-keys                 Allow approximate key matching (e.g. 'CUST-001' vs 'CUST001').
  --output TEXT                Where to write the report (default: report.md).
  --confidence-threshold FLOAT Minimum confidence to count as a pattern match (default: 0.9).
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

