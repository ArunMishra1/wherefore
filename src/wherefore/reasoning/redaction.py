"""
reasoning/redaction.py

Detects and masks common, STRUCTURALLY-RECOGNIZABLE sensitive data
patterns (emails, SSNs, credit card numbers, US phone numbers) before
any value reaches explain()'s prompt construction -- the boundary
where data crosses from "stays on the user's machine" to "sent to an
external API."

HONEST SCOPE, stated plainly because overclaiming here would be worse
than not having this feature at all: this is PATTERN-BASED detection
of STRUCTURED sensitive data with a regular, recognizable shape. It
is NOT a general PII detector. It will NOT recognize that "John Smith"
in a customer_name column is a real person's name, or that a free-text
notes field contains a home address. It catches the specific, common
categories named in this project's design discussion (emails, SSNs,
card numbers, phone numbers) because those have reliable, checkable
shapes -- not because it understands what a value MEANS.

Confirmed by direct testing against this project's OWN id formats
(ACCT-100042, PT-500003) and date strings (2024-01-15) that none of
these patterns false-positive on data this project already generates
-- a redaction layer that mangles legitimate account IDs would be
actively worse than no redaction, since it would break the tool's
usefulness while giving false reassurance about privacy.

Default behavior: ON whenever explain() is called (see explain.py's
integration). Off only via explicit --no-redact, for a user who has
already vetted their data and wants raw values in the prompt --
secure-by-default, not opt-in, since opt-in redaction tends to mean
most people never enable it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    # Confirmed by direct testing: \b doesn't transition correctly
    # before a literal '(' (it's a non-word character, so \b lands
    # AFTER the paren, right at the first digit) -- the original
    # pattern's \b\(? ordering left the opening paren OUTSIDE the
    # matched span, so re.sub's replacement left a dangling '(' in
    # the redacted output (e.g. "(555) 123-4567" -> "([REDACTED:phone]").
    # Fixed by putting the optional '(' before \b, as an explicit part
    # of what gets matched and replaced, not something \b is relied on
    # to handle.
    "phone": re.compile(r"(\+?1[-. ]?)?\(?\b\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b"),
}


@dataclass
class RedactionResult:
    redacted_value: str
    categories_found: list[str]


def redact_value(value) -> RedactionResult:
    """
    Checks a single value against all known patterns and returns a
    redacted version with each detected category replaced by a label
    like [REDACTED:email]. Non-string values are converted to string
    first (the same representation that would otherwise be sent to
    the LLM) since a value's TYPE doesn't change whether its printed
    form might contain something sensitive.

    Returns categories_found=[] and the original string unchanged if
    nothing matched -- this is the common case and should be cheap.
    """
    text = str(value)
    categories_found = []

    for category, pattern in _PATTERNS.items():
        if pattern.search(text):
            categories_found.append(category)
            text = pattern.sub(f"[REDACTED:{category}]", text)

    return RedactionResult(redacted_value=text, categories_found=categories_found)


def redact_mismatch_rows(mismatches: list) -> tuple[list, list[str]]:
    """
    Takes a list of MismatchRow-like objects (anything with
    .source_value / .target_value attributes) and returns a new list
    with both values redacted, plus a flat list of every category
    found across the whole cluster (deduplicated) -- useful for
    surfacing to the user ("redacted 3 email(s), 1 SSN-shaped value")
    rather than redacting silently with no visibility into what happened.

    Does not mutate the input list or its MismatchRow objects --
    returns new objects, same principle as the corruptors' "never
    mutate the input" contract elsewhere in this project.
    """
    from wherefore.comparison.diff_result import MismatchRow

    redacted_rows = []
    all_categories: set[str] = set()

    for m in mismatches:
        source_result = redact_value(m.source_value)
        target_result = redact_value(m.target_value)
        all_categories.update(source_result.categories_found)
        all_categories.update(target_result.categories_found)

        redacted_rows.append(
            MismatchRow(
                key=m.key,
                column=m.column,
                source_value=source_result.redacted_value,
                target_value=target_result.redacted_value,
            )
        )

    return redacted_rows, sorted(all_categories)
