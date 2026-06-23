# Design

Why `wherefore` is built the way it is — for a reader who wants the
idea before (or instead of) the code.

If you're about to work on the codebase itself,
[`ARCHITECTURE.md`](./ARCHITECTURE.md) is the file map you actually
want. This doc is the on-ramp to that one, not a replacement.

Every claim below is checked against the real code. Where intent and
reality diverge, this doc says so — see [Known gaps](#known-gaps).

## Contents

- [Design](#design)
  - [Contents](#contents)
  - [The problem](#the-problem)
  - [How a comparison flows](#how-a-comparison-flows)
  - [Key selection](#key-selection)
  - [The diff](#the-diff)
  - [Clustering](#clustering)
  - [Taxonomy as data](#taxonomy-as-data)
  - [The AI layer](#the-ai-layer)
  - [Engineering challenges](#engineering-challenges)
  - [The stack](#the-stack)
  - [Summary](#summary)
  - [Known gaps](#known-gaps)

---

## The problem

A standard diff tool answers one question: *do these two datasets
differ?* That's rarely the question that matters. After a migration or
a system cutover, the real question is *why* — and "4,000 fields don't
match" doesn't help anyone fix anything.

`wherefore` turns that into something closer to:

> Most mismatches in `created_at` share the same five-hour offset.
> This is consistent with a UTC-to-local-time transformation defect.
> Here are representative rows and the confidence of that finding.

The whole project is that move — from *detection* to *diagnosis* —
while staying honest about what's known versus inferred.

## How a comparison flows

```
Source types (CSV, JSON, Parquet, Excel, S3, SQLite, Postgres)
        ↓
Loader / database adapter
        ↓
Standard pandas DataFrame   ← everything below this line is
        ↓                     identical no matter where the data came from
Join-key resolution
        ↓
Deterministic diff (datacompy)
        ↓
Cluster mismatches, check statistical signatures
        ↓
(--explain only) Ask Claude for a plain-English narrative
        ↓
Render the report
```

**Why the DataFrame boundary matters.** Without it, every new source
type would touch the comparison engine, clustering, and reporting.
With it, a new format is purely a loader's problem. Proof:
`compare-dir`'s `db://*` batch mode reused the exact same "list every
real unit, intersect both sides" logic the file-vs-file path already
used for filenames — just swapped to table names.

## Key selection

`wherefore` resolves a join key three ways:

- **Explicit** (`--key`) — always honored.
- **Files** — a uniqueness-ratio heuristic picks a column. No
  confirmation step.
- **Databases** — reads the real schema and *requires* confirmation
  before anything runs.

That asymmetry is deliberate. A wrong guess against a CSV costs you a
re-run. A wrong guess against a real database is worse — so that path
never proceeds silently, even though its detection is more reliable,
not less, than the file heuristic.

**Fuzzy key matching** (`--fuzzy-keys`, via RapidFuzz) handles one
specific case: the same record, reformatted. The real test case is
`"EMP-1001"` becoming `"EMP1001"`. A plain diff sees one missing row
and one extra row; `wherefore` can recognize them as the same entity,
with a confidence score attached.

## The diff

The comparison layer wraps `datacompy` to produce a `DiffResult`:
matched rows, source-only rows, target-only rows, column-level
mismatches. Deliberately the least interesting part of the pipeline.

It's a dataclass contract (`DiffResult`, `MismatchRow`,
`RowPresenceRecord`), not print statements — so the CLI, reporting, and
the eval harness all consume the same thing.

## Clustering

Mismatches get grouped by column and checked against known statistical
signatures — never a guess at cause, only a shape.

| Pattern | What the signature checks |
|---|---|
| Time-zone shift | Mismatches differ from source by the same delta |
| Truncation | Target text is a shorter prefix of source text |
| Enum drift | Source values map 1-to-1 to different target values |
| Null coercion | A real null became a literal string (`"NULL"`, `"N/A"`...) |
| Float precision drift | Target equals source after a float32 round-trip |
| Encoding mismatch | Text is explainable as common mojibake |
| Deduplication failure | Extra rows duplicate matched rows |
| Key mismatch | Missing/extra rows have near-identical normalized IDs |

Every signature returns a confidence score, 0.0–1.0 — never a sentence.
Clustering's vocabulary stops at *"matches a time-shift signature,
0.95 confidence."* Causal language belongs only to the optional
explanation layer, and even there it must stay hedged.

**Two patterns work differently, by necessity.** `dedup_failure` and
`key_mismatch` aren't column mismatches — the signal is a whole row
missing or extra. They run through a separate function
(`detect_row_presence_patterns`), so the main mismatch contract didn't
need to change shape for them.

*Current gap:* neither pattern is wired into the automated eval harness
yet. Both have direct unit tests instead. Tracked in
`TAXONOMY_TODO.md`, not hidden.

## Taxonomy as data

Each failure pattern is a YAML file — name, applicable types, detection
hints, explanation guidance — validated by a Pydantic schema at
startup. Implementations stay in Python; definitions don't.

```
YAML taxonomy              Python
 ├── Pattern name            ├── Signature implementations
 ├── Applicable types         ├── Clustering
 └── Detection hints         └── Runtime dispatch
```

Adding a pattern means: write the YAML, add a signature function, add
a synthetic corruptor for test data, add eval coverage. Nothing about
clustering or reasoning code needs to change — dispatch is by data
type, mechanically, not a list someone has to remember to update.

## The AI layer

Nothing calls an external API by default. `--explain` is the one
opt-in path that does.

- **Redaction runs first, always.** Pattern-based detection for
  emails, SSNs, card numbers, US phone numbers. `--no-redact` is
  opt-out, never the default.
- **The model sees evidence, not data.** Statistical facts and a
  handful of redacted example rows — never the dataset itself.
- **The model doesn't find the pattern.** Clustering already did,
  deterministically. Claude narrates an existing finding; it can't
  invent one. This is what keeps the eval harness meaningful — it
  scores the deterministic layer, not the model's prose.

One honest limit: pattern-based redaction isn't a universal PII
detector. It catches *shaped* values like emails. It can't know "John
Smith" is a person's name. The project names this limitation rather
than implying more coverage than it has.

## Engineering challenges

**A diff is easy; the cause is hard.** Thousands of mismatches can
share one cause. → Cluster first, check signatures second, narrate
(optionally) third.

**Real data breaks in predictable ways.** Dates land as text, nulls
show up as several different strings, floats drift across precision
round-trips, CSVs lose datetime typing entirely on round-trip. →
Normalize conservatively on load; dispatch by type; don't force every
mismatch into a known cause.

**False positives are a real risk.** Flagging every 35-character field
as truncated would be actively wrong. → Only flag truncation where the
field's real modern max exceeds the suspicious legacy boundary, and
report it as a confidence score, not a verdict.

**An LLM can sound right while being wrong.** → It never picks the
pattern — only describes one already found deterministically.

**Testing a diagnosis is harder than testing that code runs.** → A
real eval harness: generate clean data, inject one known corruption,
keep the ground truth, score whether the pipeline found it. The answer
is known in advance, which is what makes the score mean something.

## The stack

| Technology | Why |
|---|---|
| Python 3.10+ | Fast iteration, strong data ecosystem |
| pandas | The shared in-memory abstraction everything assumes |
| NumPy | Numeric ops, precision analysis |
| DataComPy | Comparison foundation, not reinvented |
| RapidFuzz | Fuzzy key matching, no hand-rolled similarity logic |
| Pydantic | Validates taxonomy schema + explanation output |
| PyYAML | Makes the taxonomy data, not code |
| Typer | Typed, discoverable CLI |
| PyArrow | Parquet read/write |
| OpenPyXL | Excel read/write |
| boto3 *(optional)* | S3 source support |
| psycopg2-binary *(optional)* | Real PostgreSQL connectivity |
| Anthropic SDK *(optional)* | The `--explain` layer |
| pytest | The test suite |
| moto *(dev)* | Real mocked AWS backend for S3 tests |
| py-pglite *(dev)* | Real PostgreSQL (via WASM) for Postgres tests |

Every dependency touching an external system is an optional extra, not
core — most users never install most of these. The pattern throughout:
import lazily where needed, catch `ImportError`, re-raise with a clear
`pip install wherefore[x]` message instead of a raw error.

## Summary

`wherefore` is an explainable data-reconciliation tool focused on root
cause, not raw differences. It normalizes sources into one shared
representation, resolves keys with different caution levels for files
versus databases, diffs deterministically, clusters mismatches, and
checks them against a taxonomy of known failure patterns — each with a
confidence score, never a causal claim.

An optional layer can turn a confirmed pattern into a plain-English
explanation, after redaction, without ever inventing the finding
itself. New patterns are added through configuration and test
fixtures, never by editing the core pipeline.

It doesn't claim to eliminate migration defects or replace judgment.
It turns "4,000 fields don't match" into a specific, falsifiable
hypothesis someone can check in minutes — and stays honest about where
evidence ends and inference begins.

## Known gaps

- `dedup_failure`/`key_mismatch`: not scored by the eval harness yet
  (real unit tests exist; the gap is tracked).
- MySQL: connection-string parsing exists, connectivity doesn't.
  Calling it raises a clear `NotImplementedError`.
- `compare-dir`'s `db://*` mode: database-vs-database and
  directory-vs-directory work; mixing the two doesn't. Left unsolved
  on purpose — pairing a table name against a filename isn't
  symmetric.
- Performance at scale: an active, ongoing investigation with real
  measurements. See [`PERFORMANCE.md`](./PERFORMANCE.md) — not assumed
  fine just because small examples work.
