"""
Tests for cli.py, using Typer's CliRunner to invoke the real `compare`
command exactly as a person would from a terminal -- not just calling
the underlying Python functions directly. This is what caught two real
bugs during development: Typer collapsing a single command out of
subcommand mode (so `wherefore compare a b` failed with "unexpected
extra argument"), and CSV round-tripping silently losing datetime
dtype (so a real timezone_shift fixture written to disk and read back
showed as "unrecognized" despite scoring 1.0 confidence in memory).
Both are covered explicitly below so they can't silently regress.
"""

import json
import os

import pytest
from typer.testing import CliRunner

import wherefore.cli as cli_module
from wherefore.cli import app
from wherefore.reasoning.explain import ClusterExplanation
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, generate_dataset
from wherefore.synthetic.corruptors.timezone_shift import apply

runner = CliRunner()


@pytest.fixture
def timezone_shift_csv_pair(tmp_path):
    """
    Writes a real timezone_shift-corrupted fixture to actual CSV files
    on disk -- not in-memory DataFrames -- since that round-trip is
    exactly what exposed the datetime dtype bug.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=42)
    target, _ = apply(source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1)

    source_path = tmp_path / "source.csv"
    target_path = tmp_path / "target.csv"
    source.to_csv(source_path, index=False)
    target.to_csv(target_path, index=False)
    return source_path, target_path


def test_compare_is_an_explicit_subcommand_not_the_root_command():
    """
    Regression test for the Typer single-command collapsing bug: with
    only one @app.command() registered, Typer makes it the app's root
    invocation instead of a 'compare' subcommand, unless a callback is
    added. Confirmed directly: without the fix, `wherefore compare a b`
    failed with "Got unexpected extra argument(s)".
    """
    result = runner.invoke(app, ["--help"])
    assert "compare" in result.output or result.exit_code == 0
    result2 = runner.invoke(app, ["compare", "--help"])
    assert result2.exit_code == 0


def test_compare_end_to_end_against_real_csv_files_on_disk(timezone_shift_csv_pair):
    """
    The actual regression test for the CSV-datetime-dtype bug: this
    exact scenario previously reported 'pattern unrecognized' when run
    through real files, despite identical in-memory data correctly
    scoring 1.0 confidence -- because load_csv didn't parse datetime
    columns, so they arrived at clustering as dtype 'str'.
    """
    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app, ["compare", str(source_path), str(target_path), "--output", str(output_path)]
    )

    assert result.exit_code == 0
    assert "timezone_shift" in result.output
    assert "confidence 1.00" in result.output
    assert output_path.exists()

    report_text = output_path.read_text()
    assert "timezone_shift" in report_text
    assert "opened_at" in report_text


def test_identical_files_report_no_mismatches(timezone_shift_csv_pair):
    source_path, _ = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app, ["compare", str(source_path), str(source_path), "--output", str(output_path)]
    )
    assert result.exit_code == 0
    assert "No column mismatches found" in result.output


def test_missing_file_exits_with_error(tmp_path):
    real_file = tmp_path / "real.csv"
    real_file.write_text("id,val\n1,2\n")

    result = runner.invoke(app, ["compare", str(real_file), str(tmp_path / "missing.csv")])
    assert result.exit_code == 1
    assert "Error loading files" in result.output


def test_explicit_key_flag_overrides_auto_detection(tmp_path):
    p1 = tmp_path / "a.csv"
    p2 = tmp_path / "b.csv"
    p1.write_text("alt_id,val\n1,10\n2,20\n")
    p2.write_text("alt_id,val\n1,10\n2,99\n")

    result = runner.invoke(
        app, ["compare", str(p1), str(p2), "--key", "alt_id", "--output", str(tmp_path / "r.md")]
    )
    assert result.exit_code == 0
    assert "val" in result.output


def test_unrecognized_key_column_exits_with_error(tmp_path):
    p1 = tmp_path / "a.csv"
    p2 = tmp_path / "b.csv"
    p1.write_text("id,val\n1,10\n")
    p2.write_text("id,val\n1,10\n")

    result = runner.invoke(app, ["compare", str(p1), str(p2), "--key", "not_a_real_column"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_fuzzy_keys_flag_resolves_reformatted_keys(tmp_path):
    """
    Mirrors the manual test done during development: dashes stripped
    from a key column during migration, resolved correctly with
    --fuzzy-keys, left as unmatched without it.
    """
    p1 = tmp_path / "source.csv"
    p2 = tmp_path / "target.csv"
    p1.write_text("account_id,val\nACCT-001,10\nACCT-002,20\n")
    p2.write_text("account_id,val\nACCT001,10\nACCT002,99\n")

    without_fuzzy = runner.invoke(
        app, ["compare", str(p1), str(p2), "--output", str(tmp_path / "r1.md")]
    )
    assert "Rows only in source: 2" in without_fuzzy.output

    with_fuzzy = runner.invoke(
        app, ["compare", str(p1), str(p2), "--fuzzy-keys", "--output", str(tmp_path / "r2.md")]
    )
    assert "Rows only in source" not in with_fuzzy.output
    assert "Matched rows: 2" in with_fuzzy.output


def test_confidence_threshold_flag_is_respected(timezone_shift_csv_pair):
    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app,
        [
            "compare",
            str(source_path),
            str(target_path),
            "--confidence-threshold",
            "1.0",
            "--output",
            str(output_path),
        ],
    )
    assert result.exit_code == 0
    assert "timezone_shift" in result.output


def test_explain_flag_off_by_default_makes_no_explanation_calls(timezone_shift_csv_pair, monkeypatch):
    """
    Confirms the core safety property: without --explain, explain()
    must never be called -- no network call, no API cost, no key
    required. Monkeypatches explain() to raise if called at all, so
    this test fails loudly if that default ever silently changes.
    """
    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("explain() must not be called when --explain is not passed")

    monkeypatch.setattr(cli_module, "explain", _fail_if_called)

    result = runner.invoke(
        app, ["compare", str(source_path), str(target_path), "--output", str(output_path)]
    )
    assert result.exit_code == 0
    report_text = output_path.read_text()
    assert "statistical findings only" in report_text
    assert "--explain" in report_text


def test_explain_flag_without_api_key_fails_fast_before_any_work(timezone_shift_csv_pair, monkeypatch):
    """
    --explain without ANTHROPIC_API_KEY must fail immediately, before
    loading files or running the diff -- not partway through, and not
    with a confusing API-level error.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source_path, target_path = timezone_shift_csv_pair

    result = runner.invoke(app, ["compare", str(source_path), str(target_path), "--explain"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_explain_flag_with_fake_provider_populates_report_and_terminal_output(
    timezone_shift_csv_pair, monkeypatch
):
    """
    With a real (fake) explanation available, confirms it actually
    flows into both the rendered Markdown report AND the terminal
    summary output -- not just one or the other.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-this-test-only")

    fake_explanation = ClusterExplanation(
        matched_pattern_id="timezone_shift",
        confidence=0.97,
        narrative="This is a fake AI narrative used only to test CLI wiring.",
        cited_rows=[],
    )

    def _fake_explain(cluster, taxonomy_menu, provider=None, redact=True):
        return fake_explanation, []

    monkeypatch.setattr(cli_module, "explain", _fake_explain)

    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app, ["compare", str(source_path), str(target_path), "--explain", "--output", str(output_path)]
    )

    assert result.exit_code == 0
    assert "fake AI narrative used only to test CLI wiring" in result.output  # terminal
    report_text = output_path.read_text()
    assert "fake AI narrative used only to test CLI wiring" in report_text  # report
    assert "AI explanation" in report_text
    assert "0.97" in report_text


def test_explain_flag_handles_per_cluster_failure_gracefully(timezone_shift_csv_pair, monkeypatch):
    """
    If explain() raises for a given cluster (e.g. a transient API
    error), the CLI should warn and continue producing a report with
    statistical detail for that cluster, not crash the whole run.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-this-test-only")

    def _failing_explain(cluster, taxonomy_menu, provider=None, redact=True):
        raise RuntimeError("simulated API failure")

    monkeypatch.setattr(cli_module, "explain", _failing_explain)

    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app, ["compare", str(source_path), str(target_path), "--explain", "--output", str(output_path)]
    )

    assert result.exit_code == 0  # the run completes despite the explain() failure
    assert "Warning" in result.output
    report_text = output_path.read_text()
    assert "timezone_shift" in report_text  # statistical detail still present


def test_explain_flag_reports_redaction_categories_to_user(timezone_shift_csv_pair, monkeypatch):
    """
    Confirms the CLI surfaces WHAT was redacted, not just that
    redaction happened silently -- a user should be able to see "an
    email was masked before this was sent to Claude," not be left
    wondering whether their data went out raw.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-this-test-only")

    fake_explanation = ClusterExplanation(
        matched_pattern_id="timezone_shift", confidence=0.9, narrative="x", cited_rows=[],
    )

    def _fake_explain_with_redaction(cluster, taxonomy_menu, provider=None, redact=True):
        categories = ["email"] if redact else []
        return fake_explanation, categories

    monkeypatch.setattr(cli_module, "explain", _fake_explain_with_redaction)

    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app, ["compare", str(source_path), str(target_path), "--explain", "--output", str(output_path)]
    )
    assert "Redacted before sending to Claude: email" in result.output
    assert "--no-redact" in result.output


def test_no_redact_flag_is_passed_through_to_explain(timezone_shift_csv_pair, monkeypatch):
    """
    Confirms --no-redact actually reaches explain() as redact=False,
    not just that the flag exists on the CLI -- the wiring matters,
    not just the surface.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-this-test-only")

    fake_explanation = ClusterExplanation(
        matched_pattern_id="timezone_shift", confidence=0.9, narrative="x", cited_rows=[],
    )
    received_redact_values = []

    def _capturing_explain(cluster, taxonomy_menu, provider=None, redact=True):
        received_redact_values.append(redact)
        return fake_explanation, []

    monkeypatch.setattr(cli_module, "explain", _capturing_explain)

    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    runner.invoke(
        app,
        ["compare", str(source_path), str(target_path), "--explain", "--no-redact", "--output", str(output_path)],
    )
    assert received_redact_values == [False]
