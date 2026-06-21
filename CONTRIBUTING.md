# Contributing

New to this codebase? [`ARCHITECTURE.md`](./ARCHITECTURE.md) is the
wider map — module layout, the end-to-end pipeline, and a few real
"looks like X but is actually Y" gaps worth knowing before you dig in.
This doc covers the most common single contribution: adding a new
taxonomy pattern.

## Adding a new failure pattern to the taxonomy

This is the main way the project grows. It should require **zero
changes** to clustering, reasoning, or registry code — see
`taxonomy/schema.py` and `taxonomy/registry.py` docstrings for why this
guarantee is mechanically enforced, not just a convention.

Steps:
1. Write the corruptor function first (`synthetic/corruptors/your_pattern.py`).
   Run it against a real fixture and look at what statistical shape it
   actually produces in the diff — don't guess this in advance.
2. If the signature it produces isn't already in
   `clustering/signatures.py`, add a detector function there.
3. Write `taxonomy/patterns/your_pattern.yaml` against
   `taxonomy/schema.py`'s `PatternDefinition` — copy
   `timezone_shift.yaml` as a template.
4. Run the registry smoke test (or just `python -c
   "from wherefore.taxonomy.registry import load_all_patterns;
   print(load_all_patterns().keys())"`) to confirm it loads and
   validates.
5. Generate a labeled fixture pair using your corruptor, commit it
   under `evals/fixtures/` along with its `ground_truth.json` (see
   below on why fixtures are committed, not regenerated on demand).
6. Run the eval harness — your new pattern should now show up in
   per-pattern precision/recall.

See `TAXONOMY_TODO.md` for the current build queue and why patterns
are built one at a time (corruptor-then-YAML, not YAML-first).

## Design decisions worth knowing before you dig in

**Why `detection_hints` supports one signature per pattern, not
compound logic in YAML.** A YAML-embedded boolean-logic DSL for
combining signatures is exactly the kind of complexity that turns
"add a pattern" into "learn our query language." Patterns needing a
second confirming signal would declare one primary signature in YAML
for cheap candidate filtering and implement a `confirmation_function`
— plain Python, no schema — for the second gate. In practice, no
pattern built so far has needed this: `dedup_failure` was originally
expected to (a row-count delta signal plus a duplicate-key
confirmation), but the real implementation turned out to need only one
direct check (`duplicate_content_fraction`, comparing an unmatched
row's full content against the other side's dataset) — a good example
of a speculative design decision turning out differently once actually
built. `key_mismatch` is a second data point for the same lesson: a
fuzzy/approximate similarity score (rapidfuzz's `fuzz.ratio`) was tried
first and produced a real false positive (two unrelated keys sharing a
domain's common ID prefix scored high enough to be indistinguishable
from a genuine reformat); switching to a deterministic
normalization-equality check resolved it with one direct check, no
second confirming signature needed. See `taxonomy/schema.py` module
docstring for the `confirmation_function` mechanism, still available
if a future pattern genuinely needs it. If we pass roughly 12-15
patterns and find ourselves writing confirmation functions for most of
them, that's the signal to revisit this and design a real
multi-signature schema instead of guessing now.

**Why eval fixtures are committed to git, not generated on demand.**
The project's headline claims (e.g. "X% accuracy on Y corruption
types") are only credible if anyone can clone the repo and reproduce
them exactly, without fighting seed/version drift across machines. All
generated fixtures + their `ground_truth.json` labels live in
`evals/fixtures/` as real committed files. `evals/fixtures/regenerate.py`
(when it exists) is a deliberate, reviewed action — run it, look at the
diff, commit it — not something that happens invisibly.

**Why clustering must never make causal claims.** `clustering/`
supplies statistical observations only ("these 12 rows differ by
exactly 5 hours"). Causal attribution ("this is a timezone bug") is
the LLM's job, every time. If clustering code starts asserting causes,
the AI reasoning layer becomes decorative, and the eval harness stops
measuring anything meaningful — it would just be checking whether the
LLM repeats what clustering already concluded. This is the central
design constraint of the whole project; see `clustering/cluster_mismatches.py`.
