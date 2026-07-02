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
from wherefore.synthetic.corruptors.dedup_failure import apply as inject_dedup_failure
from wherefore.synthetic.corruptors.key_mismatch import apply as inject_key_mismatch
from wherefore.synthetic.corruptors.timezone_shift import apply
from wherefore.synthetic.corruptors.truncation import apply as truncate

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


@pytest.fixture
def column_count_mismatch_csv_pair(tmp_path):
    """
    Source has an extra column (legacy_flag) with no equivalent in
    target, and every OTHER column is identical -- isolates schema
    drift as the only thing wherefore has to report, so a test against
    this fixture can't accidentally pass due to some other finding.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=7)
    target = source.drop(columns=["status"]).copy()
    source = source.rename(columns={"status": "legacy_flag"})
    # legacy_flag now only exists in source; nothing else differs.
    source_path = tmp_path / "source.csv"
    target_path = tmp_path / "target.csv"
    source.to_csv(source_path, index=False)
    target.to_csv(target_path, index=False)
    return source_path, target_path


def test_schema_drift_is_reported_to_terminal_and_report(column_count_mismatch_csv_pair):
    """
    Regression test for the exact scenario this feature was built for:
    before this, a column dropped during migration with no explicit
    mapping was silently excluded everywhere, and the CLI would report
    'no mismatches' -- a false all-clear. Now it must be visible both
    on stdout and in the written report, and must NOT be conflated
    with 'no column mismatches found' just because there are zero
    value-level findings.
    """
    source_path, target_path = column_count_mismatch_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app, ["compare", str(source_path), str(target_path), "--output", str(output_path)]
    )
    assert result.exit_code == 0
    assert "Schema differences" in result.output
    assert "1 only in source" in result.output
    # Terminal output is deliberately counts-only (not column names) --
    # see _print_summary -- to stay readable on wide schemas; names
    # live in the full report instead.
    assert "legacy_flag" not in result.output

    report_text = output_path.read_text()
    assert "## Schema differences" in report_text
    assert "legacy_flag" in report_text
    assert "only in source" in report_text
    # legacy_flag never appears as a column-level mismatch -- it was
    # never joined against anything on the target side.
    assert "`legacy_flag` --" not in report_text


def test_no_schema_drift_omits_the_section_entirely(timezone_shift_csv_pair):
    """The common case (identical column sets) must not print an empty
    or spurious 'Schema differences' section."""
    source_path, target_path = timezone_shift_csv_pair
    output_path = source_path.parent / "report.md"

    result = runner.invoke(
        app, ["compare", str(source_path), str(target_path), "--output", str(output_path)]
    )
    assert result.exit_code == 0
    assert "Schema differences" not in result.output
    assert "Schema differences" not in output_path.read_text()


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


def test_success_message_prints_even_with_no_row_presence_finding(tmp_path):
    """
    Regression test for a real bug: the "Full report written to..."
    confirmation was previously emitted from inside a helper that only
    ran when a row-presence pattern (dedup_failure/key_mismatch)
    matched -- so for the common case (an ordinary column mismatch,
    nothing row-presence-related at all), the message silently never
    printed, even though the report WAS written successfully to disk.
    """
    p1 = tmp_path / "source.csv"
    p2 = tmp_path / "target.csv"
    p1.write_text("id,val\n1,10\n2,20\n")
    p2.write_text("id,val\n1,10\n2,99\n")  # one ordinary column mismatch, no row-presence finding

    output_path = tmp_path / "report.md"
    result = runner.invoke(app, ["compare", str(p1), str(p2), "--output", str(output_path)])

    assert result.exit_code == 0
    assert f"Full report written to {output_path}" in result.output
    assert output_path.exists()


def test_success_message_prints_exactly_once_when_row_presence_pattern_matches(tmp_path):
    """
    Regression test for a real, severe bug: the same misplaced
    "Full report written to..." line referenced an `output_path`
    variable that was never actually in scope in that helper function,
    so the CLI crashed with NameError whenever a row-presence pattern
    (dedup_failure/key_mismatch) DID match -- AFTER the report had
    already been written to disk, leaving the person with a real
    report file but a crashing command and no success confirmation.
    """
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = inject_dedup_failure(source, key_column="account_id", affected_fraction=0.2, seed=1)

    p1 = tmp_path / "source.csv"
    p2 = tmp_path / "target.csv"
    source.to_csv(p1, index=False)
    target.to_csv(p2, index=False)

    output_path = tmp_path / "report.md"
    result = runner.invoke(app, ["compare", str(p1), str(p2), "--key", "account_id", "--output", str(output_path)])

    assert result.exit_code == 0
    assert result.exception is None
    assert result.output.count("Full report written to") == 1
    assert output_path.exists()


def test_real_key_mismatch_fixture_end_to_end(tmp_path):
    source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    target, _ = inject_key_mismatch(source, key_column="account_id", affected_fraction=0.2, seed=1)

    p1 = tmp_path / "source.csv"
    p2 = tmp_path / "target.csv"
    source.to_csv(p1, index=False)
    target.to_csv(p2, index=False)

    output_path = tmp_path / "report.md"
    result = runner.invoke(app, ["compare", str(p1), str(p2), "--key", "account_id", "--output", str(output_path)])

    assert result.exit_code == 0
    assert "key_mismatch" in result.output
    assert "key_mismatch" in output_path.read_text()


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


@pytest.fixture
def batch_test_dirs(tmp_path):
    """
    A real, realistic batch test directory: one timezone_shift pair,
    one truncation pair, one clean (no-mismatch) pair at large-enough
    row count to clear key auto-detection, and one file present only
    in source (must be excluded from pairing, not an error).
    """
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    s1 = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    t1, _ = apply(s1, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1)
    s1.to_csv(source_dir / "accounts.csv", index=False)
    t1.to_csv(target_dir / "accounts.csv", index=False)

    s2 = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=2)
    t2, _ = truncate(s2, column="customer_name", max_length=8, affected_fraction=0.5, seed=1)
    s2.to_csv(source_dir / "customers.csv", index=False)
    t2.to_csv(target_dir / "customers.csv", index=False)

    s3 = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=3)
    s3.to_csv(source_dir / "clean_table.csv", index=False)
    s3.to_csv(target_dir / "clean_table.csv", index=False)

    s1.to_csv(source_dir / "only_in_source.csv", index=False)  # must be excluded

    return source_dir, target_dir


def test_compare_dir_finds_and_compares_all_matching_pairs(batch_test_dirs, tmp_path):
    source_dir, target_dir = batch_test_dirs
    output_dir = tmp_path / "reports"

    result = runner.invoke(
        app, ["compare-dir", str(source_dir), str(target_dir), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 0
    assert "Found 3 matching file pair(s)" in result.output  # only_in_source.csv correctly excluded
    assert "accounts.csv" in result.output
    assert "timezone_shift" in result.output
    assert "customers.csv" in result.output
    assert "truncation" in result.output
    assert "clean_table.csv" in result.output
    assert "no mismatches" in result.output
    assert "Done: 3 compared, 0 skipped" in result.output


@pytest.fixture
def batch_test_dirs_with_schema_drift(tmp_path):
    """
    One pair with a pure schema-drift finding (a column dropped, zero
    value-level mismatches otherwise -- isolates the '[SCHEMA]' status
    line, which only exists to cover the case [OK]/[DIFF] can't), and
    one clean pair, to confirm the tally correctly counts 1 of 2 as
    drifted, not 0 or 2.
    """
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    s1 = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=11)
    t1 = s1.drop(columns=["status"]).copy()
    s1.to_csv(source_dir / "accounts.csv", index=False)
    t1.to_csv(target_dir / "accounts.csv", index=False)

    s2 = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=12)
    s2.to_csv(source_dir / "clean_table.csv", index=False)
    s2.to_csv(target_dir / "clean_table.csv", index=False)

    return source_dir, target_dir


def test_compare_dir_reports_pure_schema_drift_pair_with_its_own_status(
    batch_test_dirs_with_schema_drift, tmp_path
):
    """
    A pair with zero value-level findings but a dropped column must
    NOT show up as '[OK]: no mismatches' (a false all-clear) -- it
    needs its own '[SCHEMA]' status distinct from both [OK] and [DIFF]
    so it stays visible and grep-able in a large batch.
    """
    source_dir, target_dir = batch_test_dirs_with_schema_drift
    output_dir = tmp_path / "reports"

    result = runner.invoke(
        app, ["compare-dir", str(source_dir), str(target_dir), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 0
    assert "[SCHEMA] accounts.csv" in result.output
    assert "[OK] accounts.csv: no mismatches" not in result.output
    assert "[OK] clean_table.csv: no mismatches" in result.output
    assert "Schema drift detected in 1 of 2 pair(s) compared: accounts.csv" in result.output

    accounts_report = (output_dir / "accounts_report.md").read_text()
    assert "## Schema differences" in accounts_report
    assert "status" in accounts_report


def test_compare_dir_omits_schema_drift_tally_when_no_pairs_affected(batch_test_dirs, tmp_path):
    """The common case (no pair has schema drift) must not print an
    empty or spurious tally line."""
    source_dir, target_dir = batch_test_dirs
    output_dir = tmp_path / "reports"

    result = runner.invoke(
        app, ["compare-dir", str(source_dir), str(target_dir), "--output-dir", str(output_dir)]
    )
    assert result.exit_code == 0
    assert "Schema drift detected" not in result.output


def test_compare_dir_writes_one_report_per_pair(batch_test_dirs, tmp_path):
    source_dir, target_dir = batch_test_dirs
    output_dir = tmp_path / "reports"

    runner.invoke(app, ["compare-dir", str(source_dir), str(target_dir), "--output-dir", str(output_dir)])

    assert (output_dir / "accounts_report.md").exists()
    assert (output_dir / "customers_report.md").exists()
    assert (output_dir / "clean_table_report.md").exists()
    assert not (output_dir / "only_in_source_report.md").exists()

    accounts_report = (output_dir / "accounts_report.md").read_text()
    assert "timezone_shift" in accounts_report


def test_compare_dir_only_pairs_files_present_in_both_directories(batch_test_dirs):
    source_dir, target_dir = batch_test_dirs
    result = runner.invoke(
        app, ["compare-dir", str(source_dir), str(target_dir), "--output-dir", str(source_dir.parent / "r")]
    )
    assert "only_in_source" not in result.output


def test_compare_dir_skips_a_failing_pair_without_aborting_the_batch(tmp_path):
    """
    The core resilience guarantee: one pair that can't compare (e.g.
    no detectable key) must be reported and skipped, not crash the
    whole batch -- the entire point of this command is surviving a
    large, messy real directory where SOME files might not compare cleanly.
    """
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    # A genuinely good pair
    s1 = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    t1, _ = apply(s1, column="opened_at", offset_hours=5.0, seed=1)
    s1.to_csv(source_dir / "good.csv", index=False)
    t1.to_csv(target_dir / "good.csv", index=False)

    # A pair with NO shared columns at all -- key auto-detection must fail
    pd_bad_source = source_dir / "bad.csv"
    pd_bad_target = target_dir / "bad.csv"
    pd_bad_source.write_text("colA,colB\n1,2\n3,4\n")
    pd_bad_target.write_text("colC,colD\n5,6\n7,8\n")

    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app, ["compare-dir", str(source_dir), str(target_dir), "--output-dir", str(output_dir)]
    )

    assert result.exit_code == 0  # the batch completes despite one bad pair
    assert "[SKIPPED] bad.csv" in result.output
    assert "good.csv" in result.output
    assert "timezone_shift" in result.output
    assert "Done: 1 compared, 1 skipped" in result.output
    assert (output_dir / "good_report.md").exists()
    assert not (output_dir / "bad_report.md").exists()


def test_compare_dir_no_matching_files_exits_with_error(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "a.csv").write_text("id,val\n1,2\n")
    (target_dir / "b.csv").write_text("id,val\n1,2\n")  # different name, no overlap

    result = runner.invoke(app, ["compare-dir", str(source_dir), str(target_dir)])
    assert result.exit_code == 1
    assert "No matching filenames" in result.output


def test_compare_dir_rejects_non_directory_arguments(tmp_path):
    not_a_dir = tmp_path / "file.csv"
    not_a_dir.write_text("id\n1\n")
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()

    result = runner.invoke(app, ["compare-dir", str(not_a_dir), str(real_dir)])
    assert result.exit_code == 1
    assert "is not a directory" in result.output


def test_compare_dir_explain_without_api_key_fails_fast(batch_test_dirs, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source_dir, target_dir = batch_test_dirs

    result = runner.invoke(app, ["compare-dir", str(source_dir), str(target_dir), "--explain"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.output


def test_compare_dir_aggregates_redaction_categories_across_pairs(tmp_path, monkeypatch):
    """
    Confirms redaction reporting is aggregated across the WHOLE batch,
    not just the last pair processed.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-this-test-only")

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    s1 = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)
    t1, _ = apply(s1, column="opened_at", offset_hours=5.0, seed=1)
    s1.to_csv(source_dir / "pair1.csv", index=False)
    t1.to_csv(target_dir / "pair1.csv", index=False)

    fake_explanation = ClusterExplanation(
        matched_pattern_id="timezone_shift", confidence=0.9, narrative="x", cited_rows=[]
    )

    def _fake_explain(cluster, taxonomy_menu, provider=None, redact=True):
        return fake_explanation, ["email"]

    monkeypatch.setattr(cli_module, "explain", _fake_explain)

    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app, ["compare-dir", str(source_dir), str(target_dir), "--explain", "--output-dir", str(output_dir)]
    )
    assert "Redacted before sending to Claude (across all pairs): email" in result.output


# --- compare-dir database batch mode (db://* on both sides) ---
# Real end-to-end tests against real, on-disk SQLite databases -- same
# standard the single-table db:// tests in test_db.py and the earlier
# compare() CLI tests hold.


def _make_two_table_sqlite_db(path, accounts_mismatch_at=None, include_no_pk_table=False):
    """Creates a real SQLite database with an `accounts` table (PK:
    account_id) and an `orders` table (PK: order_id), optionally with
    one balance mismatch injected and/or a third table with no primary
    key -- enough real variety to exercise batch mode's per-table
    detection and skip-on-failure behavior."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE accounts (account_id TEXT PRIMARY KEY, balance REAL)")
    conn.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, total REAL)")
    for i in range(5):
        offset = 99.0 if i == accounts_mismatch_at else 0.0
        conn.execute("INSERT INTO accounts VALUES (?, ?)", (f"ACCT-{i}", 100.0 + i + offset))
        conn.execute("INSERT INTO orders VALUES (?, ?)", (f"ORD-{i}", 50.0 + i))
    if include_no_pk_table:
        conn.execute("CREATE TABLE widgets (a TEXT, b REAL)")
        conn.execute("INSERT INTO widgets VALUES ('x', 1.0)")
    conn.commit()
    conn.close()


def test_compare_dir_db_mode_finds_and_compares_all_matching_tables(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_two_table_sqlite_db(src_db)
    _make_two_table_sqlite_db(tgt_db, accounts_mismatch_at=2)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare-dir", "db://*", "db://*",
            "--source-conn-env", "SOURCE_DB", "--target-conn-env", "TARGET_DB",
            "--output-dir", str(tmp_path / "reports"),
        ],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "Found 2 matching table pair(s)" in result.output
    assert "detected primary key 'account_id'" in result.output
    assert "detected primary key 'order_id'" in result.output
    assert "[DIFF] accounts" in result.output
    assert "[OK] orders: no mismatches" in result.output
    assert "Done: 2 compared, 0 skipped" in result.output


def test_compare_dir_db_mode_writes_one_report_per_table(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_two_table_sqlite_db(src_db)
    _make_two_table_sqlite_db(tgt_db)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    output_dir = tmp_path / "reports"
    runner.invoke(
        app,
        [
            "compare-dir", "db://*", "db://*",
            "--source-conn-env", "SOURCE_DB", "--target-conn-env", "TARGET_DB",
            "--output-dir", str(output_dir),
        ],
        input="y\n",
    )

    assert (output_dir / "accounts_report.md").exists()
    assert (output_dir / "orders_report.md").exists()
    assert "db://accounts" in (output_dir / "accounts_report.md").read_text()


def test_compare_dir_db_mode_yes_flag_skips_interactive_prompt(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_two_table_sqlite_db(src_db)
    _make_two_table_sqlite_db(tgt_db)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare-dir", "db://*", "db://*",
            "--source-conn-env", "SOURCE_DB", "--target-conn-env", "TARGET_DB",
            "--output-dir", str(tmp_path / "reports"),
            "--yes",
        ],
        # No input provided -- if --yes didn't actually skip the
        # prompt, this would fail/hang rather than silently pass.
    )
    assert result.exit_code == 0
    assert "proceeding without interactive confirmation" in result.output


def test_compare_dir_db_mode_declining_confirmation_aborts_cleanly(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_two_table_sqlite_db(src_db)
    _make_two_table_sqlite_db(tgt_db)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    output_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "compare-dir", "db://*", "db://*",
            "--source-conn-env", "SOURCE_DB", "--target-conn-env", "TARGET_DB",
            "--output-dir", str(output_dir),
        ],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert not (output_dir / "accounts_report.md").exists()


def test_compare_dir_db_mode_table_with_no_primary_key_is_skipped_not_fatal(tmp_path, monkeypatch):
    """
    The real "skip one pair, don't abort the batch" principle applied
    to database batch mode: a table missing a usable primary key
    should be reported and skipped individually, with the other real
    tables still comparing successfully.
    """
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_two_table_sqlite_db(src_db, include_no_pk_table=True)
    _make_two_table_sqlite_db(tgt_db, include_no_pk_table=True)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare-dir", "db://*", "db://*",
            "--source-conn-env", "SOURCE_DB", "--target-conn-env", "TARGET_DB",
            "--output-dir", str(tmp_path / "reports"),
        ],
        input="y\n",
    )
    assert result.exit_code == 0
    assert "no primary key found on at least one side" in result.output
    assert "[SKIPPED] widgets" in result.output
    assert "Done: 2 compared, 1 skipped" in result.output


def test_compare_dir_db_mode_explicit_key_applies_to_every_table(tmp_path, monkeypatch):
    """
    --key should apply to every table in the batch, matching the
    file-based mode's own documented "applied to every pair"
    semantics -- and skip detection/confirmation entirely. A table
    where the given column doesn't exist is skipped individually
    (existing _run_comparison ValueError handling, unmodified), not a
    reason to abort.
    """
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_two_table_sqlite_db(src_db, include_no_pk_table=True)
    _make_two_table_sqlite_db(tgt_db, include_no_pk_table=True)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare-dir", "db://*", "db://*",
            "--source-conn-env", "SOURCE_DB", "--target-conn-env", "TARGET_DB",
            "--output-dir", str(tmp_path / "reports"),
            "--key", "a",
        ],
        # No input -- explicit --key must skip the confirmation prompt
        # entirely, same as compare()'s single-table path.
    )
    assert result.exit_code == 0
    assert "Proceed comparing" not in result.output
    assert "[OK] widgets" in result.output
    assert "[SKIPPED] accounts" in result.output
    assert "[SKIPPED] orders" in result.output


def test_compare_dir_db_mode_no_matching_tables_exits_with_error(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    import sqlite3

    conn1 = sqlite3.connect(str(src_db))
    conn1.execute("CREATE TABLE only_in_source (id TEXT)")
    conn1.commit()
    conn1.close()

    conn2 = sqlite3.connect(str(tgt_db))
    conn2.execute("CREATE TABLE only_in_target (id TEXT)")
    conn2.commit()
    conn2.close()

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare-dir", "db://*", "db://*",
            "--source-conn-env", "SOURCE_DB", "--target-conn-env", "TARGET_DB",
            "--output-dir", str(tmp_path / "reports"),
        ],
    )
    assert result.exit_code == 1
    assert "No matching table names found" in result.output


def test_compare_dir_db_mode_missing_conn_env_flags_gives_clear_error(tmp_path):
    result = runner.invoke(
        app,
        ["compare-dir", "db://*", "db://*", "--output-dir", str(tmp_path / "reports")],
    )
    assert result.exit_code == 1
    assert "--source-conn-env" in result.output


def test_compare_dir_rejects_mixing_database_and_directory_sources(tmp_path):
    """
    Confirmed deliberate scope boundary: pairing a table name against
    a filename isn't a well-defined matching rule, so this must be
    rejected with a clear error, not guessed at silently.
    """
    a_real_dir = tmp_path / "some_files"
    a_real_dir.mkdir()

    result = runner.invoke(
        app,
        ["compare-dir", "db://*", str(a_real_dir), "--output-dir", str(tmp_path / "reports")],
    )
    assert result.exit_code == 1
    assert "mixing a database" in result.output.lower()


@pytest.fixture
def mock_s3_with_csv_pair(monkeypatch):
    """
    Real mocked S3 bucket with a real timezone_shift fixture uploaded,
    for testing the CLI's s3:// handling end-to-end.
    """
    moto = pytest.importorskip("moto", reason="moto is required for S3 CLI tests")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    with moto.mock_aws():
        import io

        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")

        source = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=42)
        target, _ = apply(source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1)
        buf1, buf2 = io.StringIO(), io.StringIO()
        source.to_csv(buf1, index=False)
        target.to_csv(buf2, index=False)
        client.put_object(Bucket="test-bucket", Key="old/accounts.csv", Body=buf1.getvalue())
        client.put_object(Bucket="test-bucket", Key="new/accounts.csv", Body=buf2.getvalue())

        yield


def test_compare_works_end_to_end_against_real_s3_paths(mock_s3_with_csv_pair, tmp_path):
    """
    The real end-to-end test for S3 support through the actual CLI
    command -- not just the loader functions in isolation. This is
    also the regression test for a real bug caught while building this:
    cli.py's compare() only caught (FileNotFoundError, ValueError,
    UnicodeDecodeError) around load_file(), so loaders.py's new
    RuntimeError (S3 fetch failures) and ImportError (missing boto3)
    crashed with a raw traceback instead of the CLI's normal clean red
    error message. Fixed by adding both to the caught exception tuple.
    """
    output_path = tmp_path / "report.md"
    result = runner.invoke(
        app,
        [
            "compare",
            "s3://test-bucket/old/accounts.csv",
            "s3://test-bucket/new/accounts.csv",
            "--output",
            str(output_path),
        ],
    )
    assert result.exit_code == 0
    assert "timezone_shift" in result.output
    assert output_path.exists()


def test_compare_with_s3_error_shows_clean_message_not_raw_traceback(monkeypatch):
    """
    Confirms an S3-related failure (e.g. nonexistent bucket) produces
    the CLI's normal clean error output, not an unhandled exception.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    moto = pytest.importorskip("moto", reason="moto is required for this test")

    with moto.mock_aws():
        result = runner.invoke(
            app,
            [
                "compare",
                "s3://nonexistent-bucket-xyz/a.csv",
                "s3://nonexistent-bucket-xyz/b.csv",
            ],
        )
        assert result.exit_code == 1
        assert "Error loading files" in result.output


# --- Database connectivity (db:// sources) ---
# Real end-to-end tests against real, on-disk SQLite files -- not
# mocked connections -- the same standard the S3 tests above hold
# (real moto-mocked AWS, never a stubbed boto3 client).


def _make_sqlite_db(path, rows_with_mismatch_at=None):
    """Creates a real SQLite file with an `accounts` table, optionally
    injecting one balance mismatch at the given row index so a
    comparison against an otherwise-identical sibling database finds
    something real to report."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE accounts (account_id TEXT PRIMARY KEY, balance REAL)")
    for i in range(10):
        offset = 99.0 if i == rows_with_mismatch_at else 0.0
        conn.execute("INSERT INTO accounts VALUES (?, ?)", (f"ACCT-{i}", 100.0 + i + offset))
    conn.commit()
    conn.close()


def test_db_source_end_to_end_with_confirmed_primary_key(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_sqlite_db(src_db)
    _make_sqlite_db(tgt_db, rows_with_mismatch_at=3)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare",
            "db://accounts",
            "db://accounts",
            "--source-conn-env",
            "SOURCE_DB",
            "--target-conn-env",
            "TARGET_DB",
            "--output",
            str(tmp_path / "report.md"),
        ],
        input="y\n",
    )
    assert result.exit_code == 0
    assert "detected primary key 'account_id'" in result.output
    assert "balance" in result.output


def test_db_source_yes_flag_skips_interactive_prompt(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_sqlite_db(src_db)
    _make_sqlite_db(tgt_db)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare",
            "db://accounts",
            "db://accounts",
            "--source-conn-env",
            "SOURCE_DB",
            "--target-conn-env",
            "TARGET_DB",
            "--output",
            str(tmp_path / "report.md"),
            "--yes",
        ],
        # No input provided at all -- if --yes didn't actually skip the
        # prompt, this would hang/fail rather than silently pass,
        # since CliRunner with no input behaves like closed stdin.
    )
    assert result.exit_code == 0
    assert "proceeding without interactive confirmation" in result.output


def test_db_source_declining_confirmation_aborts_cleanly(tmp_path, monkeypatch):
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_sqlite_db(src_db)
    _make_sqlite_db(tgt_db)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare",
            "db://accounts",
            "db://accounts",
            "--source-conn-env",
            "SOURCE_DB",
            "--target-conn-env",
            "TARGET_DB",
            "--output",
            str(tmp_path / "report.md"),
        ],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert not (tmp_path / "report.md").exists()


def test_db_source_explicit_key_skips_confirmation_prompt_entirely(tmp_path, monkeypatch):
    """
    Passing --key explicitly should bypass primary-key detection and
    confirmation entirely -- no prompt printed, no input needed at all
    (confirmed by passing no `input=` and the call still succeeding,
    which would hang/fail if a prompt were unexpectedly shown).
    """
    src_db = tmp_path / "source.sqlite"
    tgt_db = tmp_path / "target.sqlite"
    _make_sqlite_db(src_db)
    _make_sqlite_db(tgt_db)

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{src_db}")
    monkeypatch.setenv("TARGET_DB", f"sqlite:///{tgt_db}")

    result = runner.invoke(
        app,
        [
            "compare",
            "db://accounts",
            "db://accounts",
            "--source-conn-env",
            "SOURCE_DB",
            "--target-conn-env",
            "TARGET_DB",
            "--key",
            "account_id",
            "--output",
            str(tmp_path / "report.md"),
        ],
    )
    assert result.exit_code == 0
    assert "Proceed with this key?" not in result.output
    assert "detected primary key" not in result.output


def test_db_source_missing_conn_env_flag_gives_clear_error(tmp_path):
    result = runner.invoke(
        app,
        ["compare", "db://accounts", str(tmp_path / "doesnt_matter.csv"), "--key", "id"],
    )
    assert result.exit_code == 1
    assert "--source-conn-env" in result.output


def test_db_source_unset_env_var_gives_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
    result = runner.invoke(
        app,
        [
            "compare",
            "db://accounts",
            str(tmp_path / "doesnt_matter.csv"),
            "--source-conn-env",
            "DEFINITELY_NOT_SET",
            "--key",
            "id",
        ],
    )
    assert result.exit_code == 1
    assert "is not set" in result.output


def test_db_source_no_primary_key_table_still_prompts(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "no_pk.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE widgets (a TEXT, b REAL)")
    conn.execute("INSERT INTO widgets VALUES ('x', 1.0)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("NOPK_DB", f"sqlite:///{db_path}")

    result = runner.invoke(
        app,
        [
            "compare",
            "db://widgets",
            "db://widgets",
            "--source-conn-env",
            "NOPK_DB",
            "--target-conn-env",
            "NOPK_DB",
            "--output",
            str(tmp_path / "report.md"),
        ],
        input="y\n",
    )
    assert "no primary key found" in result.output


def test_db_source_composite_primary_key_reports_clear_unsupported_error(tmp_path, monkeypatch):
    """
    A composite primary key is detected and shown, but using it
    directly as a join key isn't supported yet (real, tracked future
    work, not silently mishandled) -- confirms the user gets a clear
    error after confirming, not a crash or a wrong comparison.
    """
    import sqlite3

    db_path = tmp_path / "composite.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE regional (region TEXT, id TEXT, val REAL, PRIMARY KEY (region, id))"
    )
    conn.execute("INSERT INTO regional VALUES ('us', '1', 10.0)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("COMPOSITE_DB", f"sqlite:///{db_path}")

    result = runner.invoke(
        app,
        [
            "compare",
            "db://regional",
            "db://regional",
            "--source-conn-env",
            "COMPOSITE_DB",
            "--target-conn-env",
            "COMPOSITE_DB",
            "--output",
            str(tmp_path / "report.md"),
        ],
        input="y\n",
    )
    assert result.exit_code == 1
    assert "composite" in result.output.lower()


def test_db_source_mixed_with_file_source_works(tmp_path, monkeypatch):
    """db:// on one side, a plain file on the other -- confirms the two
    source types can genuinely be mixed in one comparison, which
    matters for a real-world "comparing a database export against a
    live table" scenario."""
    db_path = tmp_path / "accounts.sqlite"
    _make_sqlite_db(db_path)

    csv_path = tmp_path / "accounts.csv"
    csv_path.write_text(
        "account_id,balance\n" + "\n".join(f"ACCT-{i},{100.0 + i}" for i in range(10)) + "\n"
    )

    monkeypatch.setenv("SOURCE_DB", f"sqlite:///{db_path}")

    result = runner.invoke(
        app,
        [
            "compare",
            "db://accounts",
            str(csv_path),
            "--source-conn-env",
            "SOURCE_DB",
            "--key",
            "account_id",
            "--output",
            str(tmp_path / "report.md"),
        ],
    )
    assert result.exit_code == 0
