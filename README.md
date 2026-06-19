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

🚧 **Early, actively built in public. Not yet usable end-to-end.**

What's real today:
- Synthetic data generator producing two realistic domains (financial
  accounts, healthcare claims), fully tested
- Comparison engine wrapping `datacompy`: schema-aware diffing,
  composite join keys, dtype-mismatch detection distinct from
  value-mismatch detection — produces a normalized `DiffResult`,
  fully tested
- Deterministic clustering: groups mismatches by column, runs
  statistical signature checks against candidate taxonomy patterns,
  outputs confidence-scored matches with **zero causal language** —
  enforced by a structural test, not just convention
- The taxonomy system: failure patterns are defined as data (YAML), not
  code, validated against a strict schema — see [Architecture](#architecture)
- One fully implemented, end-to-end-tested failure pattern:
  `timezone_shift` (corruptor → detection signature → registry → real
  diff → real cluster match, all proven against real generated
  fixtures in both domains)

What's not built yet: fuzzy key matching, the AI reasoning layer, the
eval harness scoring loop, and the CLI. See
[`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md) for the live build queue.

If you're looking at this expecting a working tool today, it isn't one
yet — watch the repo or check back. If you're a fellow builder
interested in the design (especially the taxonomy-as-data architecture
or the evals approach), the code and docs below are written for you.

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
 comparison engine        (wraps datacompy; schema-aware, fuzzy key matching)
        │
        ▼
 normalized diff result
        │
        ▼
 deterministic clustering  (groups mismatches; runs cheap statistical
        │                   signature checks — NO causal claims here)
        ▼
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
dependencies, and runs the test suite (should show **27 passed**). It's
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

### Trying what currently works

The CLI isn't wired up yet (`wherefore compare ...` will raise
`NotImplementedError` — see [Status](#status)). To actually exercise
the parts that are real:

```python
from wherefore.synthetic.base_dataset import generate_dataset, FINANCIAL_ACCOUNTS
from wherefore.synthetic.corruptors.timezone_shift import apply
from wherefore.comparison.diff_engine import compare
from wherefore.clustering.cluster_mismatches import cluster_mismatches

source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=42)
target, affected_rows = apply(source, column="opened_at", offset_hours=5.0, seed=1)

result = compare(source, target, join_columns="account_id")
clusters = cluster_mismatches(result)

for c in clusters:
    if c.is_unrecognized:
        print(f"{c.column}: {len(c.mismatches)} mismatches, no known pattern matched")
    else:
        for match in c.candidate_patterns:
            print(f"{c.column}: matches '{match.pattern_id}' (confidence {match.confidence:.2f})")
```

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

