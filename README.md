# wherefore

**Tells you *why* two datasets differ — not just that they do.**

[![CI](https://github.com/ArunMishra1/wherefore/actions/workflows/ci.yml/badge.svg)](https://github.com/ArunMishra1/wherefore/actions/workflows/ci.yml)
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
pip install -e .
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

---

### Contents

[Quickstart](#quickstart) · [Why this exists](#why-this-exists) ·
[What's built](#whats-built) · [Architecture](#architecture) ·
[Evals](#evals--why-trust-the-explanations) · [All flags](#all-flags) ·
[Contributing](#contributing)

---

## Quickstart

```bash
git clone https://github.com/ArunMishra1/wherefore.git
cd wherefore
./dev_setup.sh
```

This creates a `.venv/`, installs everything, and runs the test suite
(should show **266 passed**, no API key needed — the test suite uses a
fake AI provider, zero network calls). Safe to re-run.

Then, on any two files of yours:

```bash
wherefore compare old_export.csv new_export.csv --output report.md
```

Works the same with `.csv`, `.json`, `.parquet`, or `.xlsx`/`.xls` —
mix and match freely, format is auto-detected per file. No key column
needed; `wherefore` finds one. If it picks wrong, or you have many
table pairs to check at once (a real migration is dozens of tables,
not one), see [usage details](#usage) below.

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
| **Modes** | One file pair (`compare`) or a whole directory (`compare-dir`) |
| **Taxonomy** | 6 failure patterns built & tested: `timezone_shift`, `truncation`, `enum_drift`, `null_type_coercion`, `float_precision`, `encoding_mismatch` |
| **AI layer** | Verified against the real Claude API twice — manually and via the scored eval harness — 100% match on a small (six-fixture) sample |
| **Privacy** | Redacts emails/SSNs/cards/phones before any `--explain` call, on by default |
| **Tests** | 266 passing, including a real (mocked) S3 round-trip and end-to-end runs against real generated files |

**Not built yet:** more fixture coverage at scale, two patterns needing
clustering extensions (`key_mismatch`, `dedup_failure` -- both detect
via missing/extra rows, not column mismatches), and database connectivity
(Postgres, MySQL,
SQLite). File-based sources — local and `s3://` — and CSV/JSON/Parquet/
Excel are all supported today. Live queue: [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).

<details>
<summary>The harder bugs this surfaced, if you're curious</summary>

Building the 4th pattern (`null_type_coercion`) surfaced three real
bugs spanning the comparison engine, the file loaders, and the eval
harness itself. Building the 5th (`float_precision`) surfaced a
subtler one: a magnitude-based heuristic that looked right scored a
real false positive on an adversarial test case, fixed by checking the
underlying mechanism (an exact float32 round-trip) directly instead of
approximating its size. Full account, including how each was found and
fixed: [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).
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
[`CONTRIBUTING.md`](./CONTRIBUTING.md) for the contract.

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
in [`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).

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
[`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md).
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

### A whole migration, not one table

```bash
$ wherefore compare-dir old_exports new_exports --output-dir reports
Found 3 matching file pair(s). Comparing...

  [DIFF] accounts.csv: 1 column(s) affected (timezone_shift)
  [DIFF] patients.csv: 1 column(s) affected (truncation)
  [OK] transactions.csv: no mismatches

Done: 3 compared, 0 skipped. Reports written to reports/
```

Files are matched by **identical filename** between the two
directories — no fuzzy matching at the file level, since guessing
wrong about *which two tables* you're comparing is worse than guessing
wrong about a row key. A pair that can't be compared (bad format, no
detectable key) is skipped and reported, not fatal to the rest of the
batch. Every `compare` flag works here too, applied to every pair.

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
[`SECURITY.md`](./SECURITY.md).

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

wherefore compare-dir SOURCE_DIR TARGET_DIR [OPTIONS]

  --output-dir TEXT             Directory for one report per pair (default: reports).
  --key, --fuzzy-keys, --confidence-threshold, --explain, --no-redact   Same as `compare`, applied to every pair.
```
</details>

## Contributing

Contributions are welcome, especially new taxonomy patterns. Start
with [`CONTRIBUTING.md`](./CONTRIBUTING.md) — the pattern contract, why
patterns are built corruptor-first rather than YAML-first, and the
design decisions worth knowing before you dig in.

Found a security issue? See [`SECURITY.md`](./SECURITY.md).

## License

Apache License 2.0 — see [`LICENSE`](./LICENSE). Contributions are
accepted under the same license (see `NOTICE` for attribution).
