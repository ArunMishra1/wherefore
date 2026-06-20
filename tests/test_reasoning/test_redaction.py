"""
Tests for reasoning/redaction.py. Every pattern here was verified
against both real PII-shaped examples and this project's OWN data
formats (account IDs, patient IDs, dates) before being trusted -- a
redaction layer that mangles legitimate IDs would be worse than none.
"""

import pytest

from wherefore.comparison.diff_result import MismatchRow
from wherefore.reasoning.redaction import redact_mismatch_rows, redact_value


def test_email_is_redacted():
    result = redact_value("john.smith@example.com")
    assert result.redacted_value == "[REDACTED:email]"
    assert result.categories_found == ["email"]


def test_ssn_is_redacted():
    result = redact_value("123-45-6789")
    assert result.redacted_value == "[REDACTED:ssn]"
    assert result.categories_found == ["ssn"]


def test_credit_card_with_dashes_is_redacted():
    result = redact_value("4111-1111-1111-1111")
    assert "credit_card" in result.categories_found
    assert "4111" not in result.redacted_value


def test_credit_card_without_separators_is_redacted():
    result = redact_value("4111111111111111")
    assert "credit_card" in result.categories_found


def test_phone_with_parens_is_redacted_without_leaving_dangling_punctuation():
    """
    Regression test for a real bug caught during development: the
    original phone regex's \\b didn't transition correctly before a
    literal '(', leaving the opening parenthesis OUTSIDE the matched
    span -- so "(555) 123-4567" redacted to "([REDACTED:phone]", a
    dangling, unbalanced '(' in the output. Fixed by including the
    optional '(' explicitly in the pattern before \\b.
    """
    result = redact_value("(555) 123-4567")
    assert result.redacted_value == "[REDACTED:phone]"
    assert "(" not in result.redacted_value


def test_phone_embedded_in_sentence_redacts_only_the_number():
    result = redact_value("Call me at (555) 123-4567 anytime")
    assert result.redacted_value == "Call me at [REDACTED:phone] anytime"


def test_phone_with_country_code_is_redacted():
    result = redact_value("+1-555-123-4567")
    assert "phone" in result.categories_found


def test_own_account_id_format_is_not_redacted():
    result = redact_value("ACCT-100042")
    assert result.redacted_value == "ACCT-100042"
    assert result.categories_found == []


def test_own_patient_id_format_is_not_redacted():
    result = redact_value("PT-500003")
    assert result.redacted_value == "PT-500003"
    assert result.categories_found == []


def test_datetime_string_is_not_redacted():
    result = redact_value("2024-01-15 10:30:00")
    assert result.redacted_value == "2024-01-15 10:30:00"
    assert result.categories_found == []


def test_plain_name_is_not_redacted():
    """
    Honest limitation, not a bug: this module does NOT recognize
    "Susan Miller" as a person's name -- it only catches structured
    patterns (emails, SSNs, etc.) with a regular, recognizable shape.
    See module docstring on scope.
    """
    result = redact_value("Susan Miller")
    assert result.redacted_value == "Susan Miller"
    assert result.categories_found == []


def test_float_value_is_not_redacted():
    result = redact_value(98762.171875)
    assert "98762" in result.redacted_value
    assert result.categories_found == []


def test_known_limitation_long_numeric_id_false_positives_as_credit_card():
    """
    Documents a real, honest limitation rather than hiding it: a
    13-16 digit numeric string is indistinguishable from a credit card
    number BY SHAPE ALONE, so a long internal record/account number in
    that exact digit range will be falsely flagged. This is the
    accepted cost of pattern-based detection on bare digit sequences --
    there's no way to tell "16-digit account number" from "16-digit
    card number" without external context this module doesn't have.
    """
    result = redact_value("5000001234567890")
    assert "credit_card" in result.categories_found


def test_redact_mismatch_rows_does_not_mutate_input():
    original = [
        MismatchRow(key={"id": 1}, column="email", source_value="a@b.com", target_value="c@d.com"),
    ]
    redact_mismatch_rows(original)
    assert original[0].source_value == "a@b.com"


def test_redact_mismatch_rows_returns_redacted_copies_and_categories():
    mismatches = [
        MismatchRow(key={"id": 1}, column="email", source_value="a@b.com", target_value="c@d.com"),
        MismatchRow(key={"id": 2}, column="ssn", source_value="123-45-6789", target_value="987-65-4321"),
    ]
    redacted, categories = redact_mismatch_rows(mismatches)

    assert redacted[0].source_value == "[REDACTED:email]"
    assert redacted[0].target_value == "[REDACTED:email]"
    assert redacted[1].source_value == "[REDACTED:ssn]"
    assert categories == ["email", "ssn"]


def test_redact_mismatch_rows_with_no_sensitive_data_returns_empty_categories():
    mismatches = [
        MismatchRow(key={"id": 1}, column="status", source_value="approved", target_value="APPROVED"),
    ]
    redacted, categories = redact_mismatch_rows(mismatches)
    assert categories == []
    assert redacted[0].source_value == "approved"


def test_redact_mismatch_rows_preserves_key_and_column():
    mismatches = [
        MismatchRow(key={"id": 42}, column="email", source_value="a@b.com", target_value="c@d.com"),
    ]
    redacted, _ = redact_mismatch_rows(mismatches)
    assert redacted[0].key == {"id": 42}
    assert redacted[0].column == "email"
