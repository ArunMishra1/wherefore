"""
Tests for reasoning/explain.py. Uses a FakeProvider (not the real
Claude API) to test build_prompt(), schema validation, and error
handling deterministically -- live API behavior is tested separately
once a real key is available (see test_claude_provider.py, gated on
ANTHROPIC_API_KEY being set).
"""

import json

import pytest

from wherefore.clustering.cluster_mismatches import Cluster, PatternMatch
from wherefore.comparison.diff_result import MismatchRow
from wherefore.reasoning.explain import ClusterExplanation, build_prompt, explain
from wherefore.reasoning.providers.base import Provider
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, generate_dataset
from wherefore.synthetic.corruptors.timezone_shift import apply
from wherefore.comparison.diff_engine import compare
from wherefore.clustering.cluster_mismatches import cluster_mismatches
from wherefore.taxonomy.registry import build_llm_taxonomy_menu


class FakeProvider(Provider):
    """A Provider that returns a canned response instead of calling
    any real API -- lets explain()'s parsing/validation logic be
    tested deterministically."""

    def __init__(self, response_json: str):
        self.response_json = response_json
        self.last_system_prompt = None
        self.last_user_prompt = None

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return self.response_json


@pytest.fixture
def real_cluster():
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=42)
    target, _ = apply(source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1)
    result = compare(source, target, join_columns="account_id")
    return cluster_mismatches(result)[0]


def test_build_prompt_does_not_leak_html_comment(real_cluster):
    """
    Regression test for a real bug caught during development: the
    template file's leading HTML comment (dev-facing notes about
    prompt versioning) leaked verbatim into the system prompt sent to
    the model, since the original partition point only split on
    '# System Prompt', which appears AFTER the comment closes.
    """
    menu = build_llm_taxonomy_menu()
    system_prompt, _ = build_prompt(real_cluster, menu)
    assert "<!--" not in system_prompt
    assert "NEXT TURN" not in system_prompt  # an old TODO marker that was inside the comment


def test_build_prompt_singular_row_grammar():
    """Regression test: 'and 1 more rows not shown' was a grammar bug
    when exactly one row was truncated from the prompt."""
    mismatches = [
        MismatchRow(key={"id": i}, column="val", source_value=f"a{i}", target_value=f"b{i}")
        for i in range(9)  # MAX_EXAMPLE_ROWS_IN_PROMPT is 8, so exactly 1 is truncated
    ]
    cluster = Cluster(column="val", mismatches=mismatches, candidate_patterns=[])
    _, user_prompt = build_prompt(cluster, "no patterns")
    assert "1 more row not shown" in user_prompt
    assert "1 more rows" not in user_prompt


def test_build_prompt_includes_cluster_column_and_count(real_cluster):
    menu = build_llm_taxonomy_menu()
    _, user_prompt = build_prompt(real_cluster, menu)
    assert "opened_at" in user_prompt
    assert str(len(real_cluster.mismatches)) in user_prompt


def test_build_prompt_unrecognized_cluster_says_so_explicitly():
    cluster = Cluster(column="mystery_col", mismatches=[
        MismatchRow(key={"id": 1}, column="mystery_col", source_value="a", target_value="b"),
    ], candidate_patterns=[])
    _, user_prompt = build_prompt(cluster, "no patterns")
    assert "None" in user_prompt or "no known pattern" in user_prompt.lower()


def test_explain_returns_valid_cluster_explanation(real_cluster):
    fake_response = json.dumps({
        "matched_pattern_id": "timezone_shift",
        "confidence": 0.95,
        "narrative": "Every affected row shifted by exactly 5 hours, consistent with a timezone conversion bug.",
        "cited_rows": [
            {"key": {"account_id": "ACCT-100003"}, "source_value": "2023-01-24 16:19:51", "target_value": "2023-01-24 21:19:51"}
        ],
    })
    provider = FakeProvider(fake_response)
    result, redaction_categories = explain(real_cluster, build_llm_taxonomy_menu(), provider=provider)

    assert isinstance(result, ClusterExplanation)
    assert result.matched_pattern_id == "timezone_shift"
    assert result.confidence == 0.95
    assert len(result.cited_rows) == 1
    assert provider.last_system_prompt is not None
    assert provider.last_user_prompt is not None
    assert redaction_categories == []  # this fixture has no sensitive-looking values


def test_explain_accepts_null_matched_pattern_for_unrecognized():
    fake_response = json.dumps({
        "matched_pattern_id": None,
        "confidence": 0.1,
        "narrative": "These values don't show a consistent pattern matching any known failure mode.",
        "cited_rows": [],
    })
    cluster = Cluster(column="mystery", mismatches=[
        MismatchRow(key={"id": 1}, column="mystery", source_value="a", target_value="z"),
    ], candidate_patterns=[])
    provider = FakeProvider(fake_response)
    result, _ = explain(cluster, "no patterns", provider=provider)
    assert result.matched_pattern_id is None


def test_explain_raises_clear_error_on_malformed_provider_response(real_cluster):
    provider = FakeProvider("not valid json at all")
    with pytest.raises(ValueError, match="doesn't match ClusterExplanation"):
        explain(real_cluster, build_llm_taxonomy_menu(), provider=provider)


def test_explain_raises_on_response_missing_required_fields(real_cluster):
    incomplete_response = json.dumps({"confidence": 0.9})  # missing matched_pattern_id, narrative
    provider = FakeProvider(incomplete_response)
    with pytest.raises(ValueError, match="doesn't match ClusterExplanation"):
        explain(real_cluster, build_llm_taxonomy_menu(), provider=provider)


def test_cluster_explanation_rejects_empty_narrative():
    with pytest.raises(Exception):
        ClusterExplanation(matched_pattern_id="x", confidence=0.5, narrative="   ", cited_rows=[])


def test_cluster_explanation_rejects_out_of_range_confidence():
    with pytest.raises(Exception):
        ClusterExplanation(matched_pattern_id="x", confidence=1.5, narrative="valid text", cited_rows=[])


def test_explain_redacts_sensitive_values_before_building_prompt_by_default():
    """
    The core integration test: a cluster containing an email address
    must NOT have that raw email appear anywhere in the prompt sent to
    the provider -- only the redacted placeholder should. This is the
    real guarantee redaction exists to provide; testing it at the
    explain() level (not just redaction.py in isolation) confirms the
    wiring actually works end-to-end, not just that the function exists.
    """
    cluster = Cluster(
        column="contact_email",
        mismatches=[
            MismatchRow(key={"id": 1}, column="contact_email", source_value="old@example.com", target_value="new@example.com"),
        ],
        candidate_patterns=[],
    )
    fake_response = json.dumps({
        "matched_pattern_id": None,
        "confidence": 0.5,
        "narrative": "Email addresses changed.",
        "cited_rows": [],
    })
    provider = FakeProvider(fake_response)
    explanation, categories = explain(cluster, "no patterns", provider=provider)

    assert "old@example.com" not in provider.last_user_prompt
    assert "new@example.com" not in provider.last_user_prompt
    assert "[REDACTED:email]" in provider.last_user_prompt
    assert categories == ["email"]


def test_explain_with_redact_false_sends_raw_values():
    """
    The explicit opt-out: redact=False sends raw values, for a caller
    who has already vetted their data and wants them in the prompt.
    """
    cluster = Cluster(
        column="contact_email",
        mismatches=[
            MismatchRow(key={"id": 1}, column="contact_email", source_value="old@example.com", target_value="new@example.com"),
        ],
        candidate_patterns=[],
    )
    fake_response = json.dumps({
        "matched_pattern_id": None,
        "confidence": 0.5,
        "narrative": "Email addresses changed.",
        "cited_rows": [],
    })
    provider = FakeProvider(fake_response)
    explanation, categories = explain(cluster, "no patterns", provider=provider, redact=False)

    assert "old@example.com" in provider.last_user_prompt
    assert categories == []  # redaction never ran, so nothing to report


def test_explain_redaction_does_not_mutate_the_original_cluster():
    """
    The redacted version is used ONLY for prompt construction --
    callers (e.g. cli.py's report renderer) still need the real,
    unredacted cluster for the statistical-evidence section of the
    report, so the original cluster object must be untouched.
    """
    cluster = Cluster(
        column="contact_email",
        mismatches=[
            MismatchRow(key={"id": 1}, column="contact_email", source_value="old@example.com", target_value="new@example.com"),
        ],
        candidate_patterns=[],
    )
    fake_response = json.dumps({
        "matched_pattern_id": None, "confidence": 0.5, "narrative": "x", "cited_rows": [],
    })
    provider = FakeProvider(fake_response)
    explain(cluster, "no patterns", provider=provider)

    assert cluster.mismatches[0].source_value == "old@example.com"  # untouched


def test_explain_with_no_sensitive_data_reports_empty_categories(real_cluster):
    fake_response = json.dumps({
        "matched_pattern_id": "timezone_shift", "confidence": 0.9, "narrative": "x", "cited_rows": [],
    })
    provider = FakeProvider(fake_response)
    _, categories = explain(real_cluster, build_llm_taxonomy_menu(), provider=provider)
    assert categories == []
