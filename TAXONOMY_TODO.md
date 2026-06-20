# Taxonomy build tracker

`timezone_shift` is fully implemented end-to-end: schema + YAML +
corruptor (`synthetic/corruptors/timezone_shift.py`), proven against
the registry AND against real generated fixtures in both domains
(`FINANCIAL_ACCOUNTS`, `HEALTHCARE_PATIENTS`). The comparison engine
(`comparison/diff_engine.py`, `comparison/diff_result.py`) is also now
real -- built directly against datacompy 1.0.2's actual `PandasCompare`
API (not speculated in advance), and verified to correctly diff
`timezone_shift`-corrupted fixtures, detect dtype mismatches distinct
from value mismatches, handle composite join keys, and detect
source-only/target-only rows by key.

The clustering layer (`clustering/signatures.py`,
`clustering/cluster_mismatches.py`) is also real now -- groups
DiffResult.mismatches by column, runs the `constant_offset_subset`
signature against candidate patterns, returns statistical
PatternMatch objects only (no causal language, enforced by a
structural test). A real bug was caught and fixed while wiring this
together: `taxonomy.registry.patterns_by_dtype` originally did exact
string matching, so a YAML's `applies_to_dtypes: ["datetime"]` never
matched real pandas dtype strings like `"datetime64[s]"` -- meaning
the full pipeline silently produced "unrecognized" for every cluster
despite the signature itself scoring correctly in isolation. Fixed via
dtype-family matching; see `taxonomy/registry.py`'s
`_dtype_matches_family` and the regression tests in both
`test_registry.py` and `test_cluster_mismatches.py`.

The CLI is now real and runnable end-to-end:
`wherefore compare source.csv target.csv --output report.md` works
against actual files on disk, with `--key`, `--fuzzy-keys`, and
`--confidence-threshold` flags. Two more real bugs were caught while
building this and are documented with regression tests:

1. Typer collapses a single registered `@app.command()` into the
   app's root invocation rather than keeping it as an explicit
   subcommand -- so `wherefore compare a.csv b.csv` failed with
   "unexpected extra argument" until an empty `@app.callback()` was
   added. See `cli.py`'s `_force_subcommand_mode` and
   `test_cli.py::test_compare_is_an_explicit_subcommand_not_the_root_command`.
2. CSV has no native datetime type, so a real `timezone_shift` fixture
   written to disk and read back via `load_csv` arrived at clustering
   with dtype `'str'`, not `'datetime64[...]'` -- meaning the full
   pipeline reported "pattern unrecognized" through the real CLI even
   though the identical in-memory data scored 1.0 confidence. Fixed
   with conservative datetime auto-detection in `loaders.py`
   (`_try_parse_datetime_columns`), with a deliberate guard against a
   second false-positive risk discovered during the fix: bare numeric
   strings like "2024" parse as valid ISO8601 dates by default, which
   would have silently corrupted a genuine fiscal-year column.
   `loaders.py`'s docstring and `test_loaders.py` cover both the fix
   and the guard.

`comparison/key_matching.py` (fuzzy key resolution) is also real,
built directly against observed rapidfuzz scoring behavior: reformatted
keys (dashes stripped) reliably score ~90-95, genuinely different keys
can still score ~45 (not near zero, so a confidence FLOOR is required,
not just "pick the highest score"), and genuinely ambiguous ties
between two candidates are detected and left unmatched rather than
guessed. A known limitation is documented directly in the module's
docstring: once a source key is claimed by an earlier exact match, a
later fuzzy key may end up matched against whatever's left in the
pool even if it isn't a strong match in absolute terms.

The full pipeline (load real files -> resolve keys -> diff -> cluster
-> render report) now runs end-to-end via the actual CLI command,
verified against real files on disk, not just in-memory DataFrames.

`truncation` is also now fully implemented end-to-end (corruptor ->
signature -> YAML -> registry -> real CLI report), proven against a
real fixture and cross-checked against `timezone_shift` in the same
dataset to confirm clustering correctly distinguishes two independent
corruptions on different columns with zero cross-contamination -- see
`test_cluster_mismatches.py::test_two_independent_corruptions_are_correctly_distinguished_by_column`.
This is also the first proof that the project genuinely has more than
one pattern working at once, which matters for evals later (precision/
recall "per corruption type" requires more than one type to exist).

`enum_drift` is also now fully implemented end-to-end. A real
cross-contamination bug was caught and fixed while building it:
`consistent_value_mapping` originally scored 1.0 confidence on ANY
cluster where every distinct source value appeared exactly once --
including a pure `truncation` fixture, where every name is naturally
unique, so each "source value" was vacuously "consistent with itself."
This meant `truncation` and `enum_drift` would BOTH match the same
real truncation cluster the moment both patterns existed simultaneously
(they're both candidates for any string-dtype mismatch). Fixed by
requiring at least one source value to genuinely REPEAT in the cluster
before counting toward confidence -- a real recode is only
demonstrable as a pattern across repeated values; a column where
nothing repeats can't prove anything about consistency. See
`signatures.py`'s `consistent_value_mapping` docstring and the
regression test
`test_signatures.py::test_truncation_fixture_does_not_false_positive_on_consistent_value_mapping`.

This also broke one existing test that had baked in an assumption that
became false once `enum_drift` existed:
`test_column_with_no_matching_pattern_is_honestly_unrecognized`
originally corrupted every selected row to the SAME constant value,
which is -- correctly -- now a textbook `enum_drift` match, not an
unrecognized case. Updated to use genuinely random, non-repeating
corruption instead, which is the actual "nothing matches" scenario
this test is meant to prove. A reminder that as the taxonomy grows,
"nothing matches" fixtures need periodic re-examination -- a fixture
that's unrecognized today might become recognized tomorrow once a
new, legitimately-matching pattern is added, and that's a sign the
system is working, not a regression to suppress.

There are now three independently-working patterns proven to coexist
correctly in the same dataset with zero cross-contamination -- see
`test_cluster_mismatches.py::test_three_independent_corruptions_each_match_exactly_one_pattern`.

The full pipeline (load real files -> resolve keys -> diff -> cluster
-> render report) now runs end-to-end via the actual CLI command for
all three patterns, verified against real files on disk.

A real CI-only bug was caught and fixed after the first GitHub Actions
run: `test_financial_datetime_columns_have_second_precision_not_nanosecond_noise`
passed locally but failed on a fresh CI install across Python
3.10/3.11/3.12. Root cause: `_gen_datetime` relied on
`pd.to_datetime(raw_seconds, unit="s")`'s DEFAULT resolution inference
to produce `datetime64[s]`, but that default behavior changed across
pandas versions (confirmed via pandas-dev/pandas#55901 and related
upstream issues) -- pandas 3.0.3 (installed locally) infers `[s]` for
this call, while an earlier pandas 2.x (resolved fresh on CI, since
`pyproject.toml`'s `pandas>=2.0` floor permitted it) returns `[ns]`
instead. Fixed by explicitly forcing `.astype("datetime64[s]")` rather
than depending on inferred default behavior, plus tightening the
floor to `pandas>=2.2` as a secondary layer. A dedicated regression
test (`test_datetime_resolution_is_explicit_not_version_dependent`)
locks this in. The broader lesson: anything a test asserts a SPECIFIC
dtype/resolution on is a candidate for this exact failure mode if it
relies on a library's default inference rather than an explicit cast
-- worth a quick audit if another resolution-sensitive bug surfaces.

## Reasoning layer (reasoning/explain.py, reasoning/providers/)

The reasoning layer is now built: `explain.py` (ClusterExplanation
schema + build_prompt + explain()), `providers/base.py` (the Provider
ABC), `providers/claude.py` (real Anthropic SDK integration using
FORCED tool-use -- tool_choice={"type": "tool", "name": ...} -- so
Claude can't return free-text prose; it must call the tool with
arguments matching ClusterExplanation's schema, derived directly from
`ClusterExplanation.model_json_schema()` so the tool definition can
never silently drift from the pydantic model).

Two real bugs were caught and fixed while building this, found by
actually running build_prompt() against real cluster data (not by
inspection): the prompt template's leading HTML dev-comment was
leaking verbatim into the system prompt sent to the model (fixed by
stripping it before parsing), and a "1 more rows not shown" grammar
bug when exactly one row was truncated from the example list.

**LIVE-VERIFIED AGAINST THE REAL API.** The user ran
`scripts/test_explain_live.py` against real fixtures from all three
patterns plus a genuinely random/unrecognized case. Results, read in
full:

- `timezone_shift`: correctly identified the constant +5h offset,
  used the fact that the offset was IDENTICAL across summer and
  winter months to specifically rule out DST as the cause (a real
  causal inference, not a restatement of the statistic), and proposed
  a specific plausible mechanism (UTC timestamps misread as a local
  timezone during migration).
- `truncation`: noticed that non-ASCII names (e.g. 'Søren Brown')
  truncated at fewer visible characters than ASCII names, and used
  that to correctly refine "8-character limit" to "8-*byte* limit" --
  an inference neither the corruptor nor the signature function told
  it directly; it was read off the actual cited values.
- `enum_drift`: correctly identified the case-normalization recode,
  offered two plausible mechanisms (ETL transform vs. DB-level
  normalization) rather than overclaiming one, and flagged the real
  downstream risk (case-sensitive comparisons breaking).
- Genuinely unrecognized case (random garbage values): correctly
  refused to match ANY known pattern, explicitly reasoned through why
  enum_drift specifically didn't fit (the same source value mapped to
  DIFFERENT targets on different rows -- breaking the "consistent
  mapping" requirement), and proposed real alternative hypotheses (bad
  join, mis-wired column binding) instead of forcing a guess.

No prompt changes were made after this first real test -- the
v1 prompt template held up well across all four cases. Resisting the
urge to over-tune the prompt based on 4 examples; the eval harness
(once built) is the right tool for systematic prompt iteration, not
hand-picking a few good results and declaring victory.

The reasoning layer is now wired into the CLI behind an explicit
`--explain` flag -- off by default (so the tool stays free/key-free
for anyone trying it without committing to API cost), fails fast with
a clear message if `--explain` is passed without `ANTHROPIC_API_KEY`
set (checked before any diffing/clustering work, not partway through),
and degrades gracefully per-cluster if a single explain() call fails
(warns and continues, rather than crashing the whole run). The report
shows the AI narrative ALONGSIDE the statistical evidence, not instead
of it, by design -- a reader can verify the claim against the actual
cited rows rather than trusting it blindly.

131 tests passing.

## Future, deliberately deferred (not now)

**Multi-source-format support** (databases via SQLAlchemy, Parquet) --
currently `loaders.py` handles CSV/JSON only. Deferred because the
comparison engine already takes DataFrames, not file paths, so adding
a new source format doesn't touch comparison/clustering/taxonomy at
all -- it's a clean, separable addition any time, not something that
needs to happen before other work. Worth doing once there's a real
user asking for it.

## Why patterns are built one at a time, not all-YAML-first

A pattern's YAML (`detection_hints`, `llm_context`) and its corruptor
function are designed together, not in sequence. The detection
signature should describe what the corruptor function *actually
produces*, not a guess made before the corruptor exists. Writing all 8
YAML files up front would mean specifying statistical signatures for
encoding corruption, float precision loss, etc. without having built
or run the code that creates them -- speculative, and likely wrong in
ways that only surface once we try to detect what we described.

Build order per pattern: corruptor function -> confirm it produces the
intended statistical shape on a real fixture -> write detection_hints
to match what was actually observed -> write llm_context -> validate
against the registry (same loop just proven for timezone_shift).

## Remaining patterns (build in this order, easiest signature first)

`truncation` and `enum_drift` are done. `null_type_coercion` is next:

- [x] `truncation` -- string values cut off at a fixed length.
      Signature: target value is a literal prefix of source, strictly
      shorter -- confirmed NOT requiring a uniform cut length across
      rows (different source lengths can cut to different resulting
      lengths under one shared limit).
- [x] `enum_drift` -- lookup/enum values changed (renamed, recoded,
      e.g. "M"/"F" -> "Male"/"Female", or a status code remapping).
      Signature: a distinct source value consistently maps to the
      same target value, REQUIRING repetition (a value seen once
      proves nothing) -- this requirement was added after catching a
      real false-positive against `truncation` (see above).
- [ ] `null_type_coercion` -- nulls/blanks coerced into wrong types
      (empty string vs NaN vs 0 vs "None" the string). Signature
      candidate: target value is a known "null-like" sentinel where
      source had a real value, or vice versa.
- [ ] `float_precision` -- floating point rounding/precision loss
      during migration. Signature candidate: numeric diff magnitude is
      tiny and consistent with float32/float64 rounding, not a
      meaningful value change.
- [ ] `encoding_mismatch` -- UTF-8 vs Latin-1 (or similar) decode
      errors. Signature candidate: target string contains
      mojibake-pattern characters (specific byte-sequence artifacts)
      where source has clean text in the same field.
- [ ] `key_mismatch` -- fuzzy join issues; rows that SHOULD match
      don't, due to key formatting drift. This one is unusual: its
      "mismatch" often shows up as source-only/target-only rows rather
      than column-level mismatches, so it likely needs its own
      handling path in clustering, not just a signature.
- [ ] `dedup_failure` -- duplicate rows not collapsed during
      migration. Needs a `confirmation_function` per the schema's
      escape hatch (row-count delta signature + duplicate-key
      confirmation) -- flagged in schema.py design notes as the
      compound-signature example.

## Order rationale

`null_type_coercion` next: like `truncation` and `enum_drift`, it has
a straightforward signature (target value is a known null-like
sentinel where source had real data, or vice versa) and exercises a
NEW dtype family (nulls/type coercion spans numeric AND string
columns) before `dedup_failure`, which is the one genuinely compound
case and the real test of the `confirmation_function` escape hatch
described in schema.py. Worth specifically checking for
cross-contamination against the three existing string/numeric
patterns once built, given the real false-positive already caught
between `truncation` and `enum_drift`.
