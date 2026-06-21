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
