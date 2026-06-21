# Taxonomy

What `wherefore` can currently detect, what's planned, and how to add
a new pattern. For the build history — bugs found, design decisions,
why things are built the way they are — see
[`TAXONOMY_TODO.md`](./TAXONOMY_TODO.md), which is a deep-dive log,
not a reference doc.

### Contents

[What's built](#whats-built) ·
[What's planned](#whats-planned) ·
[How patterns work](#how-a-pattern-works) ·
[Adding a pattern](#adding-a-new-pattern)

---

## What's built

Eight patterns, each with a YAML definition, a real corruptor (so it
can be tested against labeled synthetic data), and a detection
signature. Six are column-mismatch patterns; two (`dedup_failure`,
`key_mismatch`) are structurally different — see the note below the
table.

| Pattern | What it detects | Signature approach |
|---|---|---|
| `timezone_shift` | A constant time offset across affected rows | All affected rows differ by the same delta |
| `truncation` | Values cut off at a fixed length | Target is a literal prefix of source |
| `enum_drift` | A lookup/enum value renamed or recoded | Same source value consistently maps to the same target value |
| `null_type_coercion` | A genuine null written as literal text (`"NULL"`, `"N/A"`) | One side is null, the other is a known sentinel string |
| `float_precision` | Precision lost through float32 rounding | Exact float32 round-trip check, not a magnitude estimate |
| `encoding_mismatch` | UTF-8 text misread as Latin-1 ("mojibake") | Exact reverse-transform check, not a regex |
| `dedup_failure` | A row duplicated with a new auto-generated key | Unmatched row's content exactly matches an existing row |
| `key_mismatch` | A row's join key was reformatted (`EMP-1001` vs `EMP1001`), so it never matched | Unmatched key normalizes (separators stripped, case folded) to match an unmatched key on the other side |

`dedup_failure` and `key_mismatch` are the odd ones out: their signal
never appears as a column-level mismatch — it shows up as an extra row
entirely, on both sides for `key_mismatch` (the original key on one
side, the reformatted key on the other). Both are detected through a
separate code path (`detect_row_presence_patterns`), not the
column-based dispatch the other six use. Both are fully built and
tested, but not yet wired into the automated eval harness (which
currently only scores column patterns) — a known, tracked gap, not a
hidden one.

Worth knowing: a row affected by `key_mismatch` will also legitimately
trigger `dedup_failure`'s signature — a reformatted key's row has, by
construction, content identical to its own original row elsewhere in
the data, which is exactly what `dedup_failure` checks for. Both
findings are reported; disambiguating "reformatted" from "duplicated"
is left to the reasoning layer, not guessed at by clustering. See
`cluster_mismatches.py`'s `_detect_row_presence_candidates` docstring.

**Eval results** (reproducible, no API key needed):
```bash
python3 -m evals.harness.run_eval
```
100% accuracy across all 7 column-pattern fixtures. Small sample —
proves the mechanism works, not that it's bulletproof at scale. See
the main [README](./README.md#evals--why-trust-the-explanations) for
the full results and the honest caveat.

## What's planned

- **More fixture coverage** — every pattern above is proven on one
  labeled fixture. Real statistical confidence needs more.
- **Wiring `dedup_failure`/`key_mismatch` into the eval harness** —
  both are fully built and unit-tested, but the harness only scores
  column-mismatch patterns today.
- **Database connectivity** — Postgres, MySQL, SQLite. File-based
  sources (local + `s3://`) only, today.

## How a pattern works

Every pattern is **data, not code** — a YAML file under
`src/wherefore/taxonomy/patterns/`, validated against a strict schema
(`taxonomy/schema.py`). A pattern has:

- `id`, `display_name`, `description` — what it is, in plain English
- `detection_hints` — which dtype(s) it applies to, and which
  signature function (in `clustering/signatures.py`) checks for it
- `llm_context` — guidance for the AI reasoning layer when writing the
  narrative for a match
- `synthetic_corruption` — which corruptor function
  (`synthetic/corruptors/`) generates labeled test fixtures for this
  pattern

Clustering never makes causal claims — it only ever reports
statistical observations ("these 12 rows differ by exactly 5 hours").
Deciding *why* is the AI's job. This boundary is enforced by a
structural test, not just convention.

## Adding a new pattern

Full step-by-step contract: [`CONTRIBUTING.md`](./CONTRIBUTING.md).
Short version: write the corruptor first and look at what it actually
produces before writing the signature or YAML. Every real bug caught
during this project's build (a false-positive signature, a dtype
mismatch nobody anticipated, a registry lookup silently failing) was
caught specifically because the corruptor existed first and produced
real data to test against — not because anyone guessed correctly in
advance. Also easy to miss: after writing a new signature function in
`clustering/signatures.py`, it still needs to be added to
`SIGNATURE_REGISTRY` — a separate step from defining the function itself.
