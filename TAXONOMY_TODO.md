# Taxonomy build tracker

`timezone_shift` is the only fully-implemented pattern so far (schema +
YAML proven against the registry; corruptor function still stubbed --
see synthetic/corruptors/timezone_shift.py).

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

- [ ] `truncation` -- string/numeric values cut off at a fixed length.
      Signature candidate: target values are consistently a prefix of
      source values, often at a suspiciously round length (255, 256,
      varchar limits).
- [ ] `enum_drift` -- lookup/enum values changed (renamed, recoded,
      e.g. "M"/"F" -> "Male"/"Female", or a status code remapping).
      Signature candidate: small, finite value-set on both sides, with
      a consistent one-to-one (or many-to-one) mapping between them.
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

`truncation` and `enum_drift` next: both have simple, almost purely
syntactic signatures (prefix-matching, finite value-set mapping) and
will validate the corruptor <-> YAML <-> registry loop again cheaply
before we hit `dedup_failure`, which is the one genuinely compound
case and the real test of the `confirmation_function` escape hatch
described in schema.py.
