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

`truncation`, `enum_drift`, `null_type_coercion`, and `float_precision`
are done. `encoding_mismatch` is next:

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

`encoding_mismatch` next: a genuinely new signature SHAPE again (byte-
level artifact detection in text, not magnitude or structure), and a
good candidate to validate the "real evidence before YAML" discipline
once more before `dedup_failure`, the one genuinely compound case and
the real test of the `confirmation_function` escape hatch.

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

Honest caveat: 6 fixtures is a small sample for either eval mode. A
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
