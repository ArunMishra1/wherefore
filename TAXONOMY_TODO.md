# Taxonomy build log

**This is a deep-dive history, not a reference doc.** It records real
bugs found, design decisions made (and sometimes reversed), and why
things are built the way they are — written chronologically as the
project was built. If you want to know what `wherefore` can currently
detect or how to add a pattern, start with [`TAXONOMY.md`](./TAXONOMY.md)
instead; come back here for the "why" behind any specific decision.

### Contents

- [Reasoning layer](#reasoning-layer-reasoningexplainpy-reasoningproviders)
- [Eval harness](#eval-harness-evalsfixtures-evalsharness)
- [Future, deliberately deferred](#future-deliberately-deferred-not-now)
- [Why patterns are built one at a time](#why-patterns-are-built-one-at-a-time-not-all-yaml-first)
- [Remaining patterns / build order](#remaining-patterns-build-in-this-order-easiest-signature-first)
- [Order rationale](#order-rationale)
- [null_type_coercion: three real bugs](#null_type_coercion-three-real-bugs-found-building-one-pattern)
- [float_precision: a signature-design lesson](#float_precision-a-signature-design-lesson)
- [First real --llm eval run](#first-real---llm-eval-run-against-the-actual-anthropic-api)
- [Multi-source format support (Parquet, Excel)](#multi-source-format-support-parquet-and-excel)
- [Multi-source roadmap](#multi-source-roadmap-whats-next)
- [Redaction layer](#redaction-layer-data-safety-before-any-new-connector)
- [Batch mode (compare-dir)](#batch-mode-compare-dir)
- [S3 support](#s3-support)
- [encoding_mismatch: the 6th pattern](#encoding_mismatch-the-6th-pattern)
- [Clustering extension for row-presence patterns](#clustering-extension-for-row-presence-patterns-key_mismatch-dedup_failure)
- [dedup_failure: built](#dedup_failure-built-using-the-row-presence-extension-above)
- [key_mismatch: built, with an architecture fork and a false positive](#key_mismatch-built-with-a-real-architecture-fork-and-a-real-false-positive-caught-mid-build)
- [PyPI packaging: a real bug caught before shipping](#pypi-packaging-a-real-severe-bug-caught-before-it-could-ship)
- [Database connectivity: SQLite built](#database-connectivity-sqlite-built-postgresmysql-deliberately-deferred)
- [PostgreSQL: built and verified against a real server](#postgresql-built-and-verified-against-a-real-postgres-server-not-mocked)
- [Documentation: ARCHITECTURE.md and troubleshooting.md](#documentation-architecturemd-and-troubleshootingmd-added)

---

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

## Eval harness (evals/fixtures/, evals/harness/)

The eval harness is now real, per the project's original founding
goal: don't just claim accuracy, prove it against labeled ground truth
anyone can reproduce.

`synthetic/ground_truth.py` defines `GroundTruth`/`InjectedCorruption`
(JSON-serializable, round-trip tested) and `write_fixture()` /
`load_fixture()` / `list_fixture_ids()`. `evals/fixtures/regenerate.py`
is the deliberate, reviewed script that generates committed fixtures
using the real corruptor functions -- run it, review the diff, commit
it; nothing regenerates fixtures silently. Four fixtures are committed:
one each for `timezone_shift`, `truncation`, `enum_drift`, and a
genuinely unrecognized case (random, non-matching corruption, used to
score the "honest_abstain" outcome).

`evals/harness/scoring.py` implements the outcome classification from
the original design notes -- true_positive, false_positive,
honest_abstain, false_abstain, false_negative -- and per-pattern
precision/recall, distinguishing "correctly said unrecognized" from
"confidently named the wrong pattern," which a naive right/wrong
scorer would conflate. Every metric in the mixed-batch test was
verified by hand before being locked in, including the
easy-to-get-wrong case where a single wrong prediction counts as a
false_negative for the ACTUAL pattern and a false_positive for the
WRONGLY PREDICTED pattern simultaneously.

`evals/harness/run_eval.py` has two modes, mirroring the CLI's
`--explain` precedent: a statistical mode (always runs, free, scores
clustering's signature match against ground truth) and an opt-in
`--llm` mode (real API calls, scores explain()'s matched_pattern_id
instead) -- gated by the same up-front ANTHROPIC_API_KEY check as the
CLI, so a missing key fails fast with a clean message instead of a
raw traceback (caught and fixed during this build, mirroring the
exact same UX bug pattern already fixed once in cli.py).

**First real run, statistical mode, against all 4 committed fixtures:
100% accuracy** (3 true positives, 1 honest abstain; precision=1.00,
recall=1.00 for all three patterns). This is reproducible by anyone --
`python3 -m evals.harness.run_eval` -- and is the first GENUINE
accuracy claim this project can make, as opposed to "I read a few
examples and they looked good."

142 tests passing.

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

`truncation`, `enum_drift`, `null_type_coercion`, `float_precision`,
`encoding_mismatch`, `dedup_failure`, and `key_mismatch` are all done.
The last two needed clustering extensions (see "Clustering extension
for row-presence patterns" below), not just a YAML + corruptor like
the first five -- see their dedicated "built" sections further down
for what the real implementation turned out to need versus what was
planned here:

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
- [x] `null_type_coercion` -- nulls coerced to a literal sentinel
      string ("NULL", "N/A", "None") during migration. Signature:
      one side is genuinely null (pd.isna()), the other is a known
      sentinel string, direction-agnostic. Building this pattern
      surfaced THREE real bugs across the stack -- see the dedicated
      section above for full detail. Legitimately co-matches
      enum_drift on some fixtures (a null mapping consistently to one
      sentinel string is, statistically, also a consistent value
      mapping) -- this is reported honestly, not suppressed; see
      cluster_mismatches.py's "On multiple legitimate matches".
- [x] `float_precision` -- floating point rounding/precision loss
      during migration. Signature: checks the EXACT float32
      round-trip (float(numpy.float32(source)) == target) rather than
      a relative-magnitude threshold -- see "float_precision: a
      signature-design lesson" below for why the magnitude approach
      was tried first and rejected after a real false positive.
- [x] `encoding_mismatch` -- UTF-8 vs Latin-1 decode mismatch
      ("mojibake"). Signature: checks the EXACT reverse mechanism
      directly (target.encode('latin-1').decode('utf-8') == source)
      rather than a regex over "suspicious" byte-sequence characters,
      which was the original speculative plan before the corruptor
      existed -- same "check the mechanism, not its footprint" lesson
      as float_precision. Confirmed a real, legitimate partial overlap
      with consistent_value_mapping (~0.33, not zero) on some fixtures,
      same class of honest overlap already documented for
      null_type_coercion/enum_drift -- correctly stays below threshold.
- [x] `key_mismatch` -- fuzzy join issues; rows that SHOULD match
      don't, due to key formatting drift. As anticipated here, its
      "mismatch" shows up as source-only/target-only rows, not
      column-level mismatches -- handled via the same row-presence
      clustering path as `dedup_failure`. See the dedicated "built"
      section below: the real implementation diverged from this plan
      in one way worth knowing -- a real false positive forced a
      switch from a fuzzy similarity score to a deterministic
      normalization check.
- [x] `dedup_failure` -- duplicate rows not collapsed during
      migration. Did NOT end up needing the `confirmation_function`
      escape hatch anticipated here -- see the dedicated "built"
      section below for why one direct signature
      (`duplicate_content_fraction`) turned out to be sufficient.

## Order rationale

`key_mismatch` and `dedup_failure` next: both surfaced (while starting
to design `dedup_failure`) as needing a real clustering extension, not
just a YAML + corruptor -- their "mismatch" shows up as
target_only_keys/source_only_keys (rows entirely present on one side),
which `cluster_mismatches()` currently has no path to examine at all.
See "Clustering extension for row-presence patterns" below for the
design. `dedup_failure` is also the real test of the
`confirmation_function` escape hatch (row-count delta signature +
duplicate-key confirmation) flagged in schema.py since the original
design.

## null_type_coercion: three real bugs found building one pattern

This pattern took meaningfully longer than the previous three because
it stress-tested parts of the stack the earlier patterns never
touched -- specifically, what happens when a column has MIXED dtypes
(real values alongside a literal null sentinel) across the full
pipeline, not just within one corruptor. Worth understanding all
three, since they compound: each one only became visible after fixing
the previous one.

**Bug 1 -- `diff_engine.py`: datacompy's per-row match flag is
unreliable for ANY row once a column's overall dtype differs between
source and target.** Confirmed directly: comparing [10.5, 20.5, 30.5]
(float) against ['10.5', '20.5', '99.9'] (str) makes datacompy report
ALL THREE rows as mismatched via its own `_match` column -- not just
the genuinely different one. A naive fix (compare stringified values)
gets this case right but breaks a different, earlier test
(`amount` float-vs-str, where 10.5 and '10.5' print identically but
are a real type-change mismatch that must still be reported). The
correct fix compares `(type(value), value)` pairs per cell for
dtype-mismatched columns, bypassing datacompy's flag entirely for
those columns. See `diff_engine.py`'s `_cell_is_mismatch` and three
regression tests in `test_diff_engine.py` covering: same type+value
(suppressed), same printed value but different type (still flagged),
and genuinely different values (flagged).

**Bug 2 -- `loaders.py`: the original all-or-nothing datetime parser
silently broke detection of the exact pattern it needed to support.**
A column with 49 real dates and 1 literal "NULL" string previously
failed to parse as datetime AT ALL (errors="raise" requires every
value to succeed) -- meaning the entire column stayed plain strings,
and the real dates on the unaffected side (which parsed fine, having
no sentinel) ended up a DIFFERENT dtype than the affected side. This
diluted the real signal (2 genuine mismatches) under ~49 false
type-mismatch "mismatches," pushing every signature's confidence
toward zero. Fixed with a hybrid approach: parse what's parseable as
real datetimes, preserve the original sentinel text exactly where
parsing fails, gated by a failure-rate threshold (max 20%) so a
genuinely non-date column isn't wrongly converted. Critically, this
does NOT use `errors="coerce"` naively -- coercing failures to NaT
would destroy the literal sentinel text that null_type_coercion needs
to detect in the first place.

**Bug 3 -- `evals/harness/`: scoring only the first candidate in
clustering's output tested registry insertion order, not anything
clustering actually promises.** Once both Bug 1 and Bug 2 were fixed,
the null_type_coercion fixture correctly produced TWO legitimate
candidates (null_type_coercion and enum_drift -- see "On multiple
legitimate matches" in cluster_mismatches.py). The eval harness scored
this as a false_negative purely because enum_drift happened to appear
first in an unordered list. Fixed with
`score_pattern_match_against_candidates` -- set-membership scoring for
clustering's multi-candidate output specifically, kept separate from
`score_pattern_match`'s exact-equality scoring for explain()'s output
(which DOES commit to exactly one answer, by design, via forced
tool-use -- so exact equality is the right test there, not set
membership).

170 tests passing.

## float_precision: a signature-design lesson

Worth recording on its own, since it's a good illustration of how a
plausible-sounding statistical heuristic can quietly hide a real
false-positive. The first version of `float32_precision_drift`
checked whether the RELATIVE magnitude of a mismatch's delta was
below a small threshold (motivated by direct testing showing float32
rounding loss has a relative magnitude around 1e-8, an order of
magnitude smaller than other plausible small-delta bugs). This worked
on the real fixture -- until a deliberately adversarial test case was
tried: a one-cent rounding bug on a six-figure value (98762.17 ->
98762.18). Because the BASE VALUE is large, a one-cent absolute change
has a relative magnitude small enough to fall within a reasonable
float32-precision threshold, even though 98762.17 actually rounds to
98762.171875 in float32 -- nowhere near 98762.18. The heuristic scored
this case 0.5 confidence: a real false positive.

The fix replaced the heuristic with a deterministic check: does
`float(numpy.float32(source))` EXACTLY equal the target? This isn't
approximating the mechanism, it's verifying it directly -- and it
correctly rejects the cents-rounding case (0.0 confidence) while still
correctly scoring the real float_precision fixture at 1.0. The general
lesson, worth remembering for any future signature: when a magnitude-
based threshold is standing in for a mechanism that's actually
deterministic and checkable (a specific rounding operation, a specific
encoding transformation, etc.), check the mechanism directly rather
than approximating its statistical footprint -- the approximation can
fail in exactly the cases a threshold is supposed to guard against.

189 tests passing.

## First real --llm eval run (against the actual Anthropic API)

Run by the user, against their own API key, across all 6 committed
fixtures: `python3 -m evals.harness.run_eval --llm`.

Result: 100% accuracy. Every `matched_pattern_id` explain() committed
to matched ground truth -- precision=1.00, recall=1.00 for all five
patterns, plus a correct honest_abstain on the genuinely-unrecognized
fixture.

The result worth dwelling on: `fixture_null_type_coercion_001`'s
cluster legitimately produces TWO correct statistical candidates
(`null_type_coercion` and `enum_drift` -- see "On multiple legitimate
matches" in cluster_mismatches.py). explain() is forced to commit to
exactly ONE matched_pattern_id via its tool schema, and it correctly
chose `null_type_coercion`. This is the first real evidence that the
project's central design bet -- report multiple legitimate candidates
honestly from clustering, and let the reasoning layer disambiguate
using the actual values, rather than hardcoding a priority rule into
clustering itself -- actually works in practice. It's not proof the
mechanism is bulletproof (one correct disambiguation on one fixture is
a single data point), but it's real evidence the design choice was
sound, not just defensible in theory.

Honest caveat: 7 fixtures is a small sample for either eval mode. A
100% result from 6 cases is a meaningfully weaker claim than 100% from
60 -- this run proves the MECHANISM works correctly end-to-end against
the real API, not that either layer is bulletproof at scale. Worth not
over-reading "100% twice" as more confidence than it actually warrants.
Expanding fixture coverage remains the natural next step before
leaning harder on these numbers in any public claim.

## Multi-source format support: Parquet and Excel

`loaders.py` now supports `.parquet` and `.xlsx`/`.xls`, alongside the
existing `.csv`/`.json`. Both verified end-to-end through the real
comparison pipeline (load -> diff -> cluster) and the real CLI, not
just unit-tested in isolation -- a real `timezone_shift` fixture
through Parquet, a real `truncation` fixture through Excel, a real
`enum_drift` fixture through Excel (initially scored just below
threshold on a too-small sample -- correctly conservative, not a bug;
confirmed correct at confidence 1.0 on a larger sample).

Two real, confirmed properties worth knowing:

**Parquet sidesteps the CSV round-trip bug class entirely.** Confirmed
by direct testing: a real datetime column round-trips through Parquet
with NO parsing step needed (native columnar typing), unlike CSV,
which required the hybrid datetime-detection logic in
`_try_parse_datetime_columns` to avoid the exact bugs this project hit
twice (nanosecond-precision noise, "NULL"-blocks-the-whole-column).

**Parquet has a genuine, real-world-accurate limitation:** a column
cannot hold a mix of types (e.g. a real Timestamp next to the literal
string "NULL") the way an in-memory pandas object-dtype column can --
writing such a column raises `pyarrow.lib.ArrowTypeError`, confirmed
directly. This means `null_type_coercion` corruption is only
representable in a Parquet file on a column that was ALREADY
string-typed before the sentinel was introduced, not on a natively
Parquet-typed (datetime/numeric) column. This is documented as an
honest limitation in `load_parquet`'s docstring, not worked around --
it accurately reflects that Parquet's strong typing makes this
specific failure mode genuinely less likely in real Parquet pipelines.

Excel needed the same null-preservation fix as CSV (confirmed directly
that `pd.read_excel`'s defaults collapse a literal "NULL" string and a
genuinely empty cell to the same NaN value) -- same
`keep_default_na=False, na_values=['']` fix, confirmed to transfer
cleanly.

`pyarrow` (previously only a transitive pandas dependency) and
`openpyxl` are now declared explicitly in `pyproject.toml`, following
the same "declare what you import directly" discipline established
after the earlier `numpy` omission.

199 tests passing.

## Multi-source roadmap: what's next

Per a deliberate scoping discussion: file-format expansion (this
round) is intentionally separated from database connectivity (next
round), since the latter introduces genuinely new architectural
concerns this project hasn't needed yet -- credential handling,
connection-string security, and a join-key story that needs to handle
real schema introspection, not just CSV-style uniqueness heuristics.

Agreed design for the database round, not yet built:
- A `SourceSpec` abstraction normalizing "where data comes from" into
  either a file path or a database connection + table, so loaders.py
  dispatches on it the same way it already dispatches on file
  extension -- this keeps comparison/clustering/taxonomy/explain()
  unaware of where data came from, same separation that made file-
  format expansion clean to add this round.
- CLI syntax: `db://table_name` as a lightweight source descriptor,
  paired with `--source-conn-env`/`--target-conn-env` flags pointing
  at ENVIRONMENT VARIABLE NAMES (not values) holding the real
  connection string -- so credentials never appear in argv or shell
  history.
- Primary key handling: auto-detect the real primary key from the
  database's own schema metadata when possible, but ALWAYS show the
  user what was detected and require confirmation before running the
  comparison -- a wrong auto-detected key against a real production
  database is a more serious mistake than a wrong key on a CSV.
- Planned to start with SQLite (no server setup needed, fully testable
  in this environment) before Postgres/MySQL, once the pattern's proven.

(Built later -- see "Database connectivity: SQLite built, Postgres/
MySQL deliberately deferred" further down for what the real
implementation needed versus what was planned here, including a real
bug this plan didn't anticipate.)

## Redaction layer: data safety before any new connector

Before building cloud-storage or database connectivity (the multi-
source roadmap above), a deliberate decision was made to build the
data-safety layer first: every new source type widens the gap between
"data on the user's machine" and "data that could reach the Claude
API" -- direct DB/S3 access removes the accidental privacy buffer that
"I already exported a sanitized CSV" provided today. Better to build
the safety layer once, now, than retrofit it after several connectors
already exist.

`reasoning/redaction.py` is real and tested: pattern-based detection
for emails, SSNs, credit card numbers, and US phone numbers, checked
directly against this project's OWN data formats (ACCT-100042,
PT-500003, ISO date strings) before being trusted -- none false-positive.

A real bug was caught and fixed during development: the phone regex's
`\b` word boundary doesn't transition correctly before a literal `(`
(non-word characters don't trigger `\b` the way the original pattern
assumed), so `"(555) 123-4567"` redacted to `"([REDACTED:phone]"` --  a
dangling, unbalanced parenthesis left in the output. Fixed by making
the optional `(` an explicit part of the matched span rather than
relying on `\b` to handle it; confirmed via a regression test plus a
sentence-embedded case ("Call me at (555) 123-4567 anytime").

A real, honest limitation is documented rather than hidden: a 13-16
digit numeric string is indistinguishable from a credit card number
BY SHAPE ALONE, so a long internal record/account number in that exact
digit range will be falsely flagged. There's no way to tell "16-digit
account number" from "16-digit card number" without context this
module doesn't have -- accepted as the honest cost of pattern-based
detection on bare digit sequences, tested explicitly rather than
swept under the rug.

**Integration design, worth understanding:** `explain()`'s signature
changed from returning `ClusterExplanation` to returning
`(ClusterExplanation, redaction_categories_found)`. Redaction metadata
is deliberately NOT a field on `ClusterExplanation` itself, even
though that would have been simpler to wire -- `ClusterExplanation`
is also the exact schema Claude is FORCED to populate via tool-use
(see providers/claude.py), and redaction describes a property of the
INPUT, not something the model should be asked to report about
itself. Keeping it as a separate return value preserves that
boundary. `redact=True` is the default on `explain()` itself (not
just at the CLI layer), so any future caller -- not only cli.py --
gets secure-by-default behavior without having to remember to ask for it.

Wired into the CLI as `--no-redact` (off only via explicit opt-out),
with redacted categories surfaced to the user in both the terminal
output and implicitly available for the report -- never silent, so a
false positive (see the digit-string limitation above) is at least
noticeable rather than hidden. `SECURITY.md` now has a full section on
exactly what data reaches Claude and what redaction does and doesn't
catch, written to avoid overclaiming "PII protection" when the real
claim is narrower and more honest: structured-pattern detection.

222 tests passing.

## Batch mode: compare-dir

A second CLI subcommand, `compare-dir`, compares every matching
filename across two directories in one run -- the real shape of a
migration audit (dozens of tables, not one), versus running `compare`
by hand once per pair.

Design decisions made deliberately, not defaulted into:
- Files are matched by IDENTICAL FILENAME only, no fuzzy matching at
  the file level. Guessing wrong about WHICH TWO TABLES you're
  comparing is a much worse mistake than guessing wrong about a row
  key -- which already has its own careful, opt-in --fuzzy-keys path.
  Files present in only one directory are silently excluded from
  pairing (not an error -- a real migration directory listing can
  legitimately have a new or removed table).
- A failure on any single pair (bad format, no detectable key) is
  reported and SKIPPED, not fatal to the whole batch -- confirmed via
  a real test with one genuinely good pair and one pair with no shared
  columns at all; the batch completes with exit code 0, correctly
  reporting 1 compared, 1 skipped.
- The core diff/cluster/explain logic was extracted from `compare()`
  into a shared `_run_comparison()` helper during this work, so
  `compare` and `compare-dir` share exactly one implementation of key
  detection, fuzzy matching, and redacted explain() calls -- not two
  copies that could drift apart. Caught and fixed a real ordering
  regression during the refactor: the "Calling Claude for N
  cluster(s)..." message was being printed AFTER the API calls
  happened rather than before, since it got left behind in the wrong
  function during extraction; fixed by moving it inside
  _run_comparison next to the actual loop.
- Redaction categories are aggregated ACROSS the whole batch, not
  reset per pair, so the final summary tells you everything that was
  masked across every file, not just the last one processed.

Real test fixtures used directly-generated data with multiple real
patterns (timezone_shift, truncation) plus a genuinely clean pair and
a deliberately-unmatched file, confirming the pairing, skip, and
report-writing behavior all work correctly together -- not just each
piece in isolation. 230 tests passing.

## S3 support

`loaders.py` now accepts `s3://bucket/key.ext` alongside local paths,
for all four formats (CSV, JSON, Parquet, Excel). `boto3` is an
OPTIONAL dependency (`pip install wherefore[s3]`), confirmed by direct
testing that a plain `pip install wherefore` (no extras) does NOT pull
boto3 in at all -- keeping the lightweight-by-default principle intact
for the majority of users who never touch S3.

A real, confirmed bug was designed around from the start, not
discovered after the fact: `pathlib.Path("s3://bucket/file.csv")`
silently MANGLES the URL (collapses the double slash to
"s3:/bucket/file.csv"), while `.suffix` detection still happens to
work on the mangled path -- meaning the corruption would be invisible
until the actual fetch failed or silently hit the wrong location. Every
entry point in loaders.py checks for an `s3://` prefix BEFORE ever
constructing a Path, via `_resolve_source` (returns a Path for local
files, an in-memory buffer for S3) and `_suffix_from_path_string`
(extracts the extension via plain string splitting for S3 URLs,
never via Path). `load_file`'s dispatch logic was rewritten around
this rather than patched, since the original implementation
constructed a Path unconditionally before any S3 check could happen.

Credentials use the standard AWS chain (env vars, `~/.aws/credentials`,
IAM role, `AWS_PROFILE`) via boto3's own default behavior -- wherefore
does not invent a custom credential mechanism. `NoCredentialsError`
and `ClientError` (both real, confirmed botocore exception types, not
guessed) are caught and re-raised with clearer, actionable messages.

A real bug was caught while testing the actual CLI command (not just
the loader functions in isolation): `cli.py`'s `compare()` and
`compare_dir()` only caught `(FileNotFoundError, ValueError,
UnicodeDecodeError)` around `load_file()` calls -- so the new
`RuntimeError` (S3 fetch failures) and `ImportError` (missing boto3)
crashed with a raw, unhandled traceback instead of the CLI's normal
clean red error message. Found by actually invoking the real CLI
command against a mocked S3 bucket, not by inspecting the loader code
alone -- a good example of why testing through the full stack (loader
-> CLI) catches what testing one layer in isolation can't. Fixed by
adding both exception types to the caught tuple in both commands.

All S3 behavior is tested against a REAL (mocked) AWS backend via
`moto` (a dev-only dependency, not needed for production use) --
real bucket creation, real object uploads, real fetches through
wherefore's actual loader and CLI code paths, not hand-rolled fakes.
Covers: CSV/JSON/Parquet/Excel round-trips through S3 (confirming the
buffer-based path preserves the exact same null-handling and datetime-
detection behavior as local files, not a simplified version),
malformed S3 paths, missing credentials, missing boto3, nonexistent
bucket/key, and the full CLI command end-to-end against a mocked
migration scenario (two buckets, two CSV exports, one timezone_shift
fixture).

248 tests passing.

## encoding_mismatch: the 6th pattern

Built using the same "check the mechanism, not its footprint"
discipline as float_precision: the original plan (see the now-stale
checklist entry this replaced) was a regex over mojibake-pattern
byte-sequence characters, but the actual corruptor and signature use
the EXACT reverse transform instead --
target.encode('latin-1').decode('utf-8') == source, confirmed by
direct testing this is the literal inverse of how real UTF-8-as-
Latin-1 mojibake is produced (e.g. "José" <-> "JosÃ©"), not an
approximation. A genuinely non-ASCII value (accented characters,
non-Latin scripts) is required for this corruption to do anything --
pure-ASCII values are identical in UTF-8 and Latin-1, so a row is only
reported as affected if the transform actually changed something,
same guard pattern as truncation.py and float_precision.py.

Confirmed a real, legitimate partial overlap with
consistent_value_mapping (scores ~0.33, not a hard zero) on some real
fixtures -- the synthetic name generator occasionally repeats a first
name across different last names, and a repeated source value mapping
consistently to the same mojibake target is, technically, also a
"consistent value mapping" for that subset. Same class of honest,
documented overlap as null_type_coercion/enum_drift; correctly stays
well below the 0.9 confidence threshold in practice.

Eval fixtures expanded to 7 (was 6); 266 tests passing.

## Clustering extension for row-presence patterns (key_mismatch, dedup_failure)

Starting to design `dedup_failure` surfaced a real, structural gap:
its actual signal does NOT show up in `diff_result.mismatches` at all.
Confirmed by direct testing -- concatenating a source DataFrame with a
deliberate sample of its own rows (simulating a migration re-run that
re-inserted already-migrated records) produces ZERO column-level
mismatches; the duplicated rows show up entirely as
`diff_result.target_only_keys`, since each duplicate key is treated as
"an extra row that doesn't have a matching partner" by the join logic,
not as a value that differs from anything.

This means `cluster_mismatches()` -- which currently only ever
examines `diff_result.mismatches`, grouped by column -- has NO path to
detect this at all, regardless of what signature function or YAML
exists. The same structural gap applies to `key_mismatch`: a row whose
key was reformatted and not resolved by `--fuzzy-keys` also shows up
as target_only_keys/source_only_keys, not a column mismatch.

This is real, deliberate scope beyond "add a YAML + corruptor" (the
pattern that worked for the previous five) -- needs a new concept
(tentatively: a `RowPresenceCluster`, parallel to the existing
column-based `Cluster`) and a new code path in `cluster_mismatches()`
that examines `target_only_keys`/`source_only_keys` directly. NOT YET
BUILT -- this section documents the design finding and the decision to
build it properly (confirmed with the user) rather than skip these two
patterns or build a half version. Concrete design questions still
open: how `explain()` and the CLI report render a row-presence
finding (no "source_value -> target_value" pair exists for a row that's
simply absent), and whether `dedup_failure`'s `confirmation_function`
escape hatch operates on the row-presence data directly or needs its
own dedicated check against the full DiffResult.

## dedup_failure: built, using the row-presence extension above

The clustering extension designed above is now real. `DiffResult` gained
`source_only_rows`/`target_only_rows` (full row content, not just keys
-- see `RowPresenceRecord`), and `detect_row_presence_patterns()` is a
new, separate function alongside `cluster_mismatches()` -- deliberately
NOT folded into it, so the widely-used `cluster_mismatches() ->
list[Cluster]` signature stayed completely unchanged for every existing
caller.

The corruptor models the realistic case: a duplicate row gets a NEW
auto-generated key, not the same key reused (which most diff tools,
including datacompy, already catch trivially). Detection
(`duplicate_content_fraction`) checks whether an unmatched row's full
value content exactly matches some row already present in the other
side's dataset -- confirmed correct on both the positive case (1.0
confidence on a real fixture) and the negative case (correctly
"unrecognized" on genuinely new rows, verified by using different
generator seeds so the extra rows have different content, not just
different keys).

The originally-anticipated `confirmation_function` escape hatch turned
out to be unnecessary -- `duplicate_content_fraction` is already a
single, complete check (row presence + content verification combined),
not a two-stage signature+confirmation design. Worth noting as a
real example of a speculative design decision (made before the
pattern existed) turning out differently once actually built.

Wired into the CLI: both `_render_report` and `_print_summary` now
show row-presence pattern matches directly alongside the existing
"Rows only in source/target" sections, not as a separate disconnected
block. `compare-dir`'s per-pair terminal summary was also fixed during
this work -- it previously only checked `result.clusters` to decide
[OK] vs [DIFF], which would have wrongly reported "no mismatches" for
a pure dedup_failure case (zero column mismatches, only row-presence
findings).

Known, honestly-tracked gap: `dedup_failure` is NOT yet wired into the
automated eval harness (`run_statistical_eval`/`run_llm_eval`), which
currently only scores column-mismatch `Cluster` results. Verified by
dedicated tests instead. Extending the harness to also score
row-presence clusters is tracked as real future work, not silently
assumed done.

## key_mismatch: built, with a real architecture fork and a real false positive caught mid-build

Two things surfaced while building this that changed the plan from
what was originally sketched.

**Architecture fork.** Reading the real code before writing anything
turned up TWO different things `key_mismatch` could plausibly mean,
both already half-anticipated in the codebase:

1. **Row-presence** (this section, what got built): an unmatched row's
   key normalizes to match an unmatched key on the other side --
   structurally identical to `dedup_failure`'s shape, just comparing
   KEYS instead of row VALUE CONTENT.
2. **Low-confidence accepted fuzzy match**: `key_matching.py`'s module
   docstring and `DiffResult.fuzzy_match_confidence`'s docstring both
   already described a SEPARATE, not-yet-built signal -- a row that
   `--fuzzy-keys` DID resolve, but only barely cleared the confidence
   floor, worth flagging for human review even though it was
   "resolved." `cli.py`'s `_apply_fuzzy_key_resolution` was silently
   DISCARDING this confidence data before this round (used it to
   rewrite keys, then threw the `FuzzyMatchResult` away).

Decided (confirmed with the user) to build (1) now as the real
`key_mismatch` pattern, and separately fix the confidence-discarding
issue in (2) as infrastructure -- `DiffResult.fuzzy_match_confidence`
is now actually threaded through from `_apply_fuzzy_key_resolution` ->
`compare()` -- WITHOUT inventing a detector for it yet. A first attempt
at a confidence formula for (2) (`1 - (raw_score - floor) / (100 -
floor)`) was tried and rejected before writing any detector code: it
scored a genuinely risky off-by-one-digit accepted match (e.g.
"CUST-00042" vs "CUST-00043", both very plausibly different real
records) at only 0.40 confidence -- well below the 0.9 default
threshold every other signature uses, meaning the detector would
almost never fire on the exact case it exists to catch. Shipping that
would have been worse than not building it: a detector that looks
done but silently doesn't work. (2) stays explicitly deferred.

**The false positive.** The first real implementation of (1) used
`rapidfuzz.fuzz.ratio` with `key_matching.py`'s own 75.0 accept floor
as the threshold -- the same scorer and floor `--fuzzy-keys` already
uses, which seemed like the obviously consistent choice. Confirmed by
direct testing that this produces a real false positive: two
GENUINELY UNRELATED keys from the same synthetic domain (different
generator seeds, so different underlying records) share a long common
ID prefix/format by construction -- e.g. `"ACCT-100002"` vs
`"ACCT-100022"`, different accounts, not a reformat of one another --
and score ~91 against each other, indistinguishable from a genuine
reformat's ~93-95. There was no clean gap to threshold on; tuning the
number wouldn't have fixed it.

Fixed by switching to a deterministic check instead of an approximate
score: does the key NORMALIZE (separators stripped, case folded) to
the exact same string as a key on the other side? Either two keys are
the same record's key under that transform, or they aren't -- no
gradient to mistune. This mirrors `float32_precision_drift` and
`mojibake_reversible` elsewhere in the taxonomy, both of which check
the literal reverse mechanism rather than approximating it with a
score/magnitude heuristic, for the same reason. Re-tested against the
same false-positive pair (now correctly 0.0) and the real positive
fixture (still 1.0) before considering this done.

**Legitimate overlap with `dedup_failure`, confirmed not a bug.** A row
whose key was merely reformatted has, by construction, non-key VALUES
identical to its own original row -- which is exactly what
`duplicate_content_fraction` checks for too. Both signatures correctly
fire at confidence 1.0 on the same `key_mismatch` fixture. This isn't
suppressed: disambiguating "reformatted" from "duplicated under a
coincidentally similar new key" needs the keys themselves compared,
which is a judgment call left to the reasoning layer, same principle
as the existing `null_sentinel_coercion`/`consistent_value_mapping`
multi-match design. Covered by a dedicated regression test, plus a
separate regression test confirming `dedup_failure`'s OWN fixture
(a duplicate under an unrelated new key like `DUPE-0`) does NOT
spuriously trigger `key_mismatch`.

**A real, separate bug found and fixed while reading `cli.py` for
context, unrelated to `key_mismatch` itself:** `_print_row_presence_match`
contained `typer.secho(f"Full report written to {output_path}", ...)`
-- but that function doesn't take `output_path` as a parameter, and
the line was misplaced inside a per-side helper instead of
`_print_summary`. Confirmed by direct testing this was a real,
reproducible bug, not a hypothetical: with no row-presence match (the
common case), the success message silently never printed even though
the report WAS written; with a row-presence match (e.g.
`dedup_failure`), the CLI CRASHED with `NameError` -- after already
writing the report to disk. No existing test caught this because no
CLI test exercised `compare` against a real `dedup_failure` fixture
end-to-end. Fixed by moving the message to the end of `_print_summary`
(where `output_path` is actually in scope) so it prints exactly once,
unconditionally. Covered by two new regression tests.

Wired into the CLI exactly the same way `dedup_failure` was -- zero
changes needed to `_render_report`, which already renders any
row-presence `candidate_patterns` entry generically regardless of
`pattern_id`.

Same honestly-tracked gap as `dedup_failure`: `key_mismatch` is NOT yet
wired into the automated eval harness. Verified by dedicated tests
instead.

316 tests passing.

## PyPI packaging: a real, severe bug caught before it could ship

Before touching Homebrew (which needs a real PyPI release or tagged
GitHub release to point a formula at), did the PyPI packaging prep
first. This surfaced a genuine, previously-invisible bug.

**The bug.** Built a real wheel (`python3 -m build --wheel`) and
installed it into a clean venv -- something no prior round had done,
since every test so far ran against an editable install
(`pip install -e .`), which reads `src/` directly off disk regardless
of packaging metadata. The installed CLI crashed immediately with
`TaxonomyLoadError: Patterns directory not found` on the very first
`compare` run. Confirmed the cause by listing the wheel's actual
contents: ALL EIGHT `taxonomy/patterns/*.yaml` files were silently
missing -- `setuptools` only includes `.py` files by default unless
package data is explicitly declared, and nothing in `pyproject.toml`
declared it. Found a second instance of the identical bug while
searching for any other non-`.py` file under `src/wherefore/`:
`reasoning/prompts/cluster_explanation_v1.md`, the `--explain` prompt
template, with the same failure mode on that code path.

**The fix.** Added `[tool.setuptools.package-data]` to `pyproject.toml`
declaring both glob patterns. Rebuilt the wheel, confirmed both file
types now present in the archive, then re-ran the full clean-venv
install test: a real `compare` run now succeeds, including the
`key_mismatch`/`dedup_failure` row-presence path; `--explain` now
fails for the correct, intended reason (missing `ANTHROPIC_API_KEY`),
not a missing template file. Built the `sdist` too (PyPI publishing
guides are explicit that a wheel alone isn't sufficient -- dependents
building from source need it) and confirmed it independently contains
both file types.

**Secondary fixes found while completing the metadata:**
- Added `project.urls` (Homepage/Repository/Issues), keywords, and
  explicit per-version Python classifiers (3.10-3.13) -- none of this
  existed before; PyPI's listing page would have shown a bare,
  unhelpful project page otherwise.
- Caught a real TOML structural bug while adding `project.urls`:
  placing the new table between `classifiers` and `dependencies`
  (instead of after the whole `[project]` table's keys) silently
  nested `dependencies` inside `[project.urls]` instead of leaving it
  in `[project]`. `python3 -m build` failed loudly and immediately
  with a clear validation error (`project.urls.dependencies` must be
  string) -- exactly the kind of fast, loud failure that makes this
  safe to fix immediately rather than something that could have
  shipped silently broken.
- `README.md`'s relative file links (`./TAXONOMY.md` etc.) were
  confirmed (via real, tracked PyPA `readme_renderer`/`packaging-problems`
  issues) to break on PyPI's standalone long-description rendering,
  since there's no relative base to resolve against there the way
  there is on GitHub. Rewrote all 9 of them to absolute
  `github.com/tracelore/wherefore/blob/main/...` URLs. Left the
  relative links BETWEEN other markdown files (e.g.
  `TAXONOMY.md` -> `./TAXONOMY_TODO.md`) untouched -- those are correct
  as-is on GitHub, where PyPI's rendering quirk doesn't apply.

**Validated with `twine check`** (PASSED on both `sdist` and `wheel`)
and a full clean-venv real install + real `compare` run, not just by
inspecting the build log.

**What couldn't be verified locally, and why:** whether the project
name `wherefore` is actually available on PyPI (no fetch access to
arbitrary `pypi.org/project/...` URLs without a prior matching search
result -- search results were inconclusive), and whether a Homebrew
formula actually installs (no `brew` binary, no macOS, no network path
to Homebrew's infrastructure from this environment). Both require the
user's own PyPI account/local machine to confirm -- the dist artifacts
and exact upload commands were handed off rather than guessed at as
"done."

(Both were later confirmed live by the user: PyPI publish succeeded
and was verified with a real clean-venv `pip install wherefore`;
Homebrew needed five real, distinct build-environment fixes across
five attempts before a successful bottle, documented in the tap
repo's own README, not here.)

## Database connectivity: SQLite built, Postgres/MySQL deliberately deferred

Per the roadmap's own stated plan: start with SQLite (no server,
stdlib `sqlite3`, fully testable in this sandbox) before Postgres/
MySQL, proving the `SourceSpec`-style abstraction works end-to-end
first.

**Architecture, confirmed against the real existing code before
writing anything:** `loaders.py`'s `_is_s3_path` check-before-`Path()`
discipline was the model to follow one layer up -- `db://table_name`
is a CLI-only source syntax, checked in a new `_load_source` dispatch
function in `cli.py` BEFORE `load_file()` ever sees the string, the
same way `s3://` is checked before any path construction. `_run_comparison`
itself needed ZERO changes -- confirmed by reading it first: it already
only takes plain DataFrames, completely agnostic to source. New module:
`comparison/db.py` -- `DatabaseBackend` enum, `ConnectionInfo`
dataclass (with a password-redacting `__repr__`, confirmed this
matters since a leaked credential in a traceback/log is exactly the
failure mode the whole env-var design exists to prevent), and SQLite-
specific `connect`/`list_columns`/`detect_primary_key`/`query_table`.
Postgres/MySQL schemes are recognized and parsed (generic-from-day-one,
per explicit discussion with the user) but `connect`/etc. raise a
clear `NotImplementedError` for them, not a silent wrong-looking
success.

**Real bug caught and fixed mid-build: `urlparse` does not match
SQLAlchemy's own documented SQLite connection-string convention.**
The de facto standard (confirmed via SQLAlchemy's own docs) is
`sqlite:///relative/path` (3 slashes) vs `sqlite:////absolute/path`
(4 slashes, the 4th being the path's own leading slash). The first
draft of this module used `urllib.parse.urlparse(...).path` to extract
the path, on the assumption that the empty-netloc/path split would
"just work" the same way it does for `s3://`. Confirmed by direct
testing that it does NOT: for the 4-slash absolute case, `urlparse`
returns a path with a DOUBLED leading slash (`//absolute/path`, not
`/absolute/path`); for the 3-slash relative case, it returns a path
with a SPURIOUS leading slash (`/relative/path`, not the intended
`relative/path`) -- neither is usable as-is, and the mismatch is real,
not a corner case (it broke the very first absolute-path test written
for this module). Fixed by abandoning `urlparse` for the SQLite path
specifically and doing a literal string-prefix strip of exactly
`"sqlite:///"` (3 slashes) -- whatever remains IS the path exactly as
the user intended it, verified against all three real shapes
(relative, absolute, and a path with no leading slash at all).
Non-SQLite schemes (Postgres/MySQL) don't have this ambiguity --
they have a real host/port/database structure `urlparse` parses
correctly -- so they're unaffected and still go through `urlparse`
normally.

**Real bugs avoided, confirmed by direct testing before assuming
anything:**
- A literal `"NULL"` string vs. a genuine SQL `NULL` -- the same
  distinction `load_csv` needs `keep_default_na=False`/`na_values=[""]`
  to preserve for CSV -- comes for FREE from `pd.read_sql` against
  SQLite, confirmed directly: SQLite's native per-value typing already
  keeps them distinct. No special-casing needed in `query_table`.
- SQLite has no native datetime type (everything is TEXT/INTEGER/REAL)
  -- confirmed directly that a datetime column round-trips through
  SQLite as plain TEXT, the IDENTICAL problem CSV has. `query_table`
  reuses `loaders.py`'s `_try_parse_datetime_columns` directly (not a
  reimplementation) for exactly this reason -- it already correctly
  parses real dates while preserving a literal sentinel string
  untouched, which is precisely what a SQLite-sourced DataFrame needs
  too. Confirmed this reuse works end-to-end against a real on-disk
  SQLite file, not just asserted by analogy.
- `PRAGMA table_info`'s primary-key column (`pk`, the 6th field) is a
  nonzero, SEQUENTIAL position for composite keys, not just a boolean
  -- confirmed directly against a real composite-PK table before
  writing `detect_primary_key`, so a composite key comes back in its
  real key order, not just "yes there's a PK."

**Primary-key confirmation, the higher-stakes UX divergence from
files, built deliberately asymmetric per the roadmap's own reasoning:**
files auto-detect a join key SILENTLY today (a uniqueness-ratio
heuristic, `_auto_detect_key`, with no confirmation prompt at all) --
confirmed by re-reading that existing code before assuming database
detection should work the same way. For `db://` sources, the roadmap
explicitly calls for MORE caution ("a wrong auto-detected key against
a real production database is a more serious mistake than a wrong key
on a CSV"), so this is a genuinely different UX, not a copy: the real
primary key is read from the database's own schema (not a heuristic
guess), shown to the user explicitly, and requires interactive
confirmation (`typer.confirm`) before anything runs -- skippable with
`--yes` for scripted use, bypassed entirely if `--key` is passed
explicitly. A composite (multi-column) key is detected and shown but
reported as not-yet-usable as an actual join key (real, tracked future
work -- `join_columns` support for `db://` specifically -- not
silently mishandled by guessing which single column to use).

**Tests:** 30 new tests in `tests/test_comparison/test_db.py`
(connection-string parsing including the slash-counting regression,
credential redaction, real on-disk SQLite primary-key/schema/null/
datetime behavior, `NotImplementedError` paths for Postgres/MySQL) plus
9 new CLI-level tests in `tests/test_cli/test_cli.py` (the full
confirm/decline/`--yes`/explicit-`--key` flows, missing-env-var and
missing-flag errors, no-PK and composite-PK paths, and a mixed
database-plus-file comparison) -- all run against real SQLite files via
`CliRunner`, the same standard the existing S3 tests hold (real moto-
mocked AWS, never a stubbed client).

316 -> 355 tests passing.

**Not built, deliberately:** Postgres and MySQL connectivity itself
(`connect`/`list_columns`/`detect_primary_key`/`query_table` all raise
clear `NotImplementedError` for these backends, not silent wrong
behavior); a `db` extra in `pyproject.toml` for their drivers
(psycopg2/PyMySQL) -- not added speculatively ahead of those backends
actually being implemented, same "don't pay for what you don't use"
principle the existing `s3` extra already follows; `db://` support in
`compare-dir` -- confirmed by reading that command's real code that it
is genuinely directory/filesystem-based (`_match_files_by_name`, real
`Path.is_dir()` checks), not a natural fit for database sources, and
deliberately not bolted on without a real design pass of its own.

## PostgreSQL: built and verified against a REAL Postgres server, not mocked

Version bumped 0.1.0 -> 0.2.0 first -- the previous version was
already live on PyPI and tagged on GitHub; shipping a genuinely new
feature (new CLI flags, a new module) under the same version number
would leave `pip install --upgrade` with nothing to do.

**Found a real way to test this honestly, not just mocked.**
`py-pglite` runs PGlite (a real PostgreSQL compiled to WASM) via
Node.js/npm, both confirmed available in this environment. This let
Postgres connectivity get the SAME verification standard SQLite got
(a real on-disk file/server, not a stub) -- real SQL execution, real
system-catalog schema introspection (`pg_index`/`pg_attribute`,
`information_schema.columns`), confirmed directly before writing any
of `db.py`'s Postgres branch.

**Real dependency tradeoff, decided explicitly with the user after
testing both options, not guessed:** psycopg2's own documentation says
a published package "shouldn't use psycopg2-binary as a module
dependency" (risk: multiple packages bundling conflicting `libpq`
copies in one environment) -- but confirmed by direct testing that
plain `psycopg2` (source) FAILS to install in a clean environment
without `libpq-dev`/`pg_config` and a C compiler already present, a
near-certain failure for most users, not a theoretical edge case.
Decided on `psycopg2-binary` anyway: `wherefore` is a single-purpose
CLI, not a multi-Postgres-dependency web app, so the library-conflict
risk the official warning targets barely applies, while the
install-failure risk of the alternative is immediate and real.

**Real, deliberate tradeoff on pandas + psycopg2:** `pd.read_sql`
against a raw psycopg2 connection (not a SQLAlchemy engine) prints
"pandas only supports SQLAlchemy connectable... Other DBAPI2 objects
are not tested." Confirmed by direct testing the warning is accurate
about being untested upstream but NOT accurate about producing wrong
results here (verified correct NULL handling and datetime typing
despite it). Discussed with the user: adding SQLAlchemy as a
dependency just to silence an accurate-but-cosmetic warning was
rejected in favor of the smaller footprint. The warning is suppressed
explicitly, scoped to only this one call, with a comment as the record
of why -- not silently swallowed.

**Confirmed by direct testing, things that come for free on Postgres
that needed real workarounds on SQLite:**
- A native `TIMESTAMP` column round-trips through `pd.read_sql` as a
  genuine `datetime64` dtype directly, with NO parsing step needed at
  all -- closer to Parquet's behavior than SQLite's TEXT-typed dates.
  `query_table`'s Postgres branch correspondingly does NOT call
  `_try_parse_datetime_columns` at all; there's nothing TEXT-typed to fix.
- The literal `"NULL"`-string-vs-genuine-NULL distinction comes for
  free here too, for the same underlying reason (native per-value typing).

**Real bugs found and fixed while building the PGlite-backed test
suite, all genuine limitations of the TEST TOOL, not of `db.py`'s real
connectivity logic -- confirmed by testing `connect()` against the
real failure in isolation each time before concluding which side the
bug was on:**
- PGlite's DSN is Unix-socket-only with NO "port" key at all --
  inventing a port value when constructing a test `ConnectionInfo`
  broke the connection ("server didn't return client encoding").
  Fixed in the TEST fixture (`port=None` when absent), not in `db.py`,
  since `psycopg2.connect(port=None, ...)` already correctly falls
  back to the default -- nothing to fix in the real code.
- PGlite is genuinely single-connection-only, confirmed in BOTH socket
  mode and TCP mode: a second `connect()` call to the same PGlite
  instance fails ("server didn't return client encoding"), and even a
  FAILED connection attempt (wrong password) consumes/wedges the one
  available slot, breaking every later test sharing that server
  instance. Fixed by switching the shared test fixture from
  "one connection per test" to "one connection, reused, for the whole
  module" -- confirmed this is a PGlite limitation, not a `db.py` bug,
  by testing `connect()` against a real multi-connection-capable
  scenario in isolation.
- PGlite does not enforce password authentication AT ALL -- confirmed
  directly that connecting with a deliberately wrong password
  succeeds. A test for "wrong password is rejected" against PGlite
  could therefore never actually fail, testing nothing real. Replaced
  with a test against a genuinely unreachable host/port instead, which
  exercises the identical `OperationalError`-catching code path in
  `connect()` without depending on auth behavior this specific tool
  doesn't implement.
- PGlite's minimal TCP mode doesn't correctly negotiate Postgres's SSL
  handshake, requiring `sslmode=disable` to connect over TCP at all.
  This surfaced a REAL, previously-undiscovered gap in `db.py` itself
  (not just a test-tool quirk): `parse_connection_string` was silently
  DISCARDING a connection string's query parameters entirely --
  `ConnectionInfo` had no field for them. Fixed properly, not just
  worked around: added `ConnectionInfo.options` (parsed from the query
  string via `urllib.parse.parse_qs`), passed through as
  `**info.options` to `psycopg2.connect()`. This is a genuine feature
  now, not just a test fix -- real managed/cloud Postgres deployments
  commonly need `sslmode` or other options specified too, for reasons
  unrelated to PGlite's specific limitation.
- A true end-to-end CLI test using a real `postgresql://` URI string
  (rather than constructing `ConnectionInfo` directly) turned out to be
  UNACHIEVABLE against PGlite specifically, confirmed by tracing the
  actual failure: `cli.py` legitimately opens TWO separate connections
  per `db://` source (one for primary-key detection, one for the
  query) -- completely normal for a real multi-connection Postgres
  server, but impossible against PGlite's single-connection limit.
  Accepted as a real, documented constraint of the test tool rather
  than continuing to fight it; the unit-level coverage (44 tests in
  `test_db.py`, all passing against the real server) already proves
  the actual logic correctly.

**Tests:** 14 new real-server PostgreSQL tests (connection, schema
introspection including composite keys, null/datetime handling, the
suppressed-warning scope, reserved-word table names) plus 4 new
unit-level tests for the `options`/`sslmode` feature (parsing,
defaulting, and confirming `connect()` actually forwards them via a
mocked `psycopg2.connect` call, since testing this against a
real SSL-requiring server isn't available in this sandbox).

355 -> 369 tests passing.

**Not built, still:** MySQL connectivity (unchanged from before --
still a clear `NotImplementedError`); `db://` support in `compare-dir`
(unchanged, same reasoning as before); a true end-to-end CLI test with
a real `postgresql://` URI string (confirmed unachievable against
PGlite specifically, not abandoned without investigation -- see above).

## Documentation: ARCHITECTURE.md and troubleshooting.md added

Two new docs, added after explicit discussion with the user about
scope. The user's first framing ("add a CLAUDE.md / skills") was
clarified before building anything: the real goal was "help any
contributor -- human or a future AI session -- ramp up on this
codebase faster," not Claude-Code-tool-specific config files in a
public open-source repo (which would be operational scaffolding for
an AI assistant, not something a real `pip install wherefore` user
would ever want or need to see). Built and named for what they
actually do instead:

- `ARCHITECTURE.md`: the wider orientation doc `CONTRIBUTING.md` never
  tried to be (that file is scoped specifically to "adding a taxonomy
  pattern"). Module map, the end-to-end pipeline, and a "things that
  look like one thing but are another" section listing real, confirmed
  gaps (the dead `reasoning/report.py` stub; the deliberately
  asymmetric file-vs-database key auto-detection; the eval harness's
  real CI-discoverability quirk) -- every claim in it checked against
  the actual current code before being written, not assumed from memory
  of having built it.
- `troubleshooting.md`: real, specific failure modes with their actual
  verified error message text (checked against the real source for
  every quoted message, not reconstructed from memory) -- not generic
  advice. Caught one real, honest gap while writing it: the doc's first
  draft suggested a `--encoding` CLI flag for non-UTF-8 files that
  turns out NOT to exist on the CLI at all (the underlying
  `load_csv(encoding=...)` parameter is real, but cli.py never exposes
  it as an option) -- fixed by documenting the actual gap honestly
  instead of describing a feature that doesn't exist.
