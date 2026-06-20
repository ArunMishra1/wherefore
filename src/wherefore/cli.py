"""
cli.py

The `wherefore compare` command. Wires together everything that's
real: loaders -> (exact or fuzzy key resolution) -> diff_engine ->
cluster_mismatches -> a Markdown report.

By default, the report shows statistical findings only -- zero
network calls, zero API cost, no key required. This is the default
specifically so anyone can clone the repo and try the tool for free
without needing an Anthropic API key (see README "Try it yourself").

Pass --explain to additionally call the real AI reasoning layer
(explain()) for each cluster and include its plain-English narrative
in the report ALONGSIDE the statistical detail -- not replacing it,
so a reader can see both the AI's causal claim and the raw evidence it
reasoned from side by side, rather than trusting the narrative blindly.
--explain requires ANTHROPIC_API_KEY to be set; this is checked up
front, before any diffing/clustering work, so a missing key fails fast
with a clear message instead of partway through a run.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from wherefore.clustering.cluster_mismatches import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    Cluster,
    cluster_mismatches,
)
from wherefore.comparison.diff_engine import compare as run_diff
from wherefore.comparison.key_matching import fuzzy_match_keys
from wherefore.comparison.loaders import load_file
from wherefore.reasoning.explain import ClusterExplanation, explain
from wherefore.taxonomy.registry import build_llm_taxonomy_menu

app = typer.Typer()


# This empty callback exists purely so Typer keeps `compare` as an
# explicit subcommand. Without it, Typer collapses a single registered
# @app.command() into the app's root invocation -- confirmed directly:
# `wherefore compare a.csv b.csv` failed with "unexpected extra
# argument" until this was added, because Typer treated `compare` as
# the literal first positional argument rather than a subcommand name.
# Remove this once a second subcommand is added (Typer stops
# collapsing once there are 2+ commands registered).
@app.callback()
def _force_subcommand_mode() -> None:
    """wherefore: explains why two datasets differ, not just that they do."""

MIN_KEY_UNIQUENESS = 0.95  # a candidate join key column must be at least this unique to be auto-selected


from dataclasses import dataclass


@dataclass
class ComparisonRunResult:
    """
    Structured output of running one source/target comparison, shared
    by `compare` (one pair) and `compare_dir` (many pairs) so the
    actual diff/cluster/explain logic lives in exactly one place.
    Render/print/write decisions stay with the caller -- this dataclass
    just carries what happened.
    """

    join_column: str
    diff_result: object
    clusters: list
    explanations: dict
    redaction_categories: set[str]


def _run_comparison(
    source_df,
    target_df,
    key: str | None,
    fuzzy_keys: bool,
    confidence_threshold: float,
    explain_flag: bool,
    no_redact: bool,
) -> ComparisonRunResult:
    """
    The actual diff -> cluster -> (optional) explain pipeline, extracted
    from compare() so compare_dir() can reuse it exactly rather than
    duplicating key-detection, fuzzy-matching, and redaction-wired
    explain() logic across two commands. Raises typer.Exit on the same
    error conditions compare() always has (no key found, key missing
    from a file) -- callers decide whether to abort the whole run or
    catch and continue (compare_dir does the latter, per-pair).
    """
    join_column = key or _auto_detect_key(source_df, target_df)
    if join_column is None:
        raise ValueError("Could not auto-detect a join key column. Pass one explicitly with --key.")

    if join_column not in source_df.columns or join_column not in target_df.columns:
        raise ValueError(f"Key column {join_column!r} not found in both files.")

    if fuzzy_keys:
        source_df, target_df = _apply_fuzzy_key_resolution(source_df, target_df, join_column)

    diff_result = run_diff(source_df, target_df, join_columns=join_column)
    clusters = cluster_mismatches(diff_result, confidence_threshold=confidence_threshold)

    explanations: dict[str, ClusterExplanation] = {}
    all_redaction_categories: set[str] = set()
    if explain_flag and clusters:
        taxonomy_menu = build_llm_taxonomy_menu()
        typer.echo(f"Calling Claude for {len(clusters)} cluster(s)...")
        for cluster in clusters:
            try:
                explanation, categories = explain(cluster, taxonomy_menu, redact=not no_redact)
                explanations[cluster.column] = explanation
                all_redaction_categories.update(categories)
            except Exception as e:
                typer.secho(
                    f"Warning: explain() failed for column {cluster.column!r}: {e}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )

    return ComparisonRunResult(
        join_column=join_column,
        diff_result=diff_result,
        clusters=clusters,
        explanations=explanations,
        redaction_categories=all_redaction_categories,
    )


@app.command()
def compare(
    source: str = typer.Argument(..., help="Path to the source CSV/JSON file"),
    target: str = typer.Argument(..., help="Path to the target CSV/JSON file"),
    key: str = typer.Option(
        None, "--key", help="Join key column name. If omitted, wherefore tries to auto-detect one."
    ),
    fuzzy_keys: bool = typer.Option(
        False,
        "--fuzzy-keys",
        help="Allow approximate key matching when exact keys don't align (e.g. 'CUST-001' vs 'CUST001').",
    ),
    output: str = typer.Option("report.md", "--output", help="Path to write the Markdown report."),
    confidence_threshold: float = typer.Option(
        DEFAULT_CONFIDENCE_THRESHOLD,
        "--confidence-threshold",
        help="Minimum confidence (0-1) for a statistical signature to count as a pattern match.",
    ),
    explain_flag: bool = typer.Option(
        False,
        "--explain",
        help="Call the real Claude API to generate plain-English causal narratives for each "
        "cluster, in addition to the statistical detail. Requires ANTHROPIC_API_KEY to be "
        "set. Makes real network calls and incurs real API cost -- off by default.",
    ),
    no_redact: bool = typer.Option(
        False,
        "--no-redact",
        help="Disable automatic redaction of common sensitive patterns (emails, SSNs, credit "
        "card numbers, phone numbers) before sending values to the Claude API with --explain. "
        "Redaction is ON by default -- only disable this if you've already vetted your data.",
    ),
) -> None:
    """
    Compare two datasets and show what's different, grouped by pattern
    where a statistical signature matches a known failure mode.

    Example:
        wherefore compare old_export.csv new_export.csv --output report.md
        wherefore compare old_export.csv new_export.csv --explain
    """
    if explain_flag and not os.environ.get("ANTHROPIC_API_KEY"):
        typer.secho(
            "Error: --explain requires ANTHROPIC_API_KEY to be set in your environment.\n"
            'Run: export ANTHROPIC_API_KEY="sk-ant-..." before using --explain.',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        source_df = load_file(source)
        target_df = load_file(target)
    except (FileNotFoundError, ValueError, UnicodeDecodeError) as e:
        typer.secho(f"Error loading files: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        result = _run_comparison(
            source_df, target_df, key, fuzzy_keys, confidence_threshold, explain_flag, no_redact
        )
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if result.redaction_categories:
        typer.secho(
            f"Redacted before sending to Claude: {', '.join(sorted(result.redaction_categories))} "
            f"-- pass --no-redact to disable this.",
            fg=typer.colors.YELLOW,
        )

    report = _render_report(
        source, target, result.join_column, result.diff_result, result.clusters, result.explanations
    )
    Path(output).write_text(report)

    _print_summary(result.diff_result, result.clusters, output, result.explanations)


@app.command(name="compare-dir")
def compare_dir(
    source_dir: str = typer.Argument(..., help="Directory of source files."),
    target_dir: str = typer.Argument(..., help="Directory of target files with matching filenames."),
    output_dir: str = typer.Option(
        "reports", "--output-dir", help="Directory to write one report per matched file pair."
    ),
    key: str = typer.Option(
        None, "--key", help="Join key column name, applied to every pair. If omitted, auto-detected per pair."
    ),
    fuzzy_keys: bool = typer.Option(False, "--fuzzy-keys", help="Allow approximate key matching, applied to every pair."),
    confidence_threshold: float = typer.Option(
        DEFAULT_CONFIDENCE_THRESHOLD, "--confidence-threshold", help="Minimum confidence for a pattern match."
    ),
    explain_flag: bool = typer.Option(
        False, "--explain", help="Call the real Claude API for every pair with mismatches. Requires ANTHROPIC_API_KEY."
    ),
    no_redact: bool = typer.Option(False, "--no-redact", help="Disable redaction for all pairs."),
) -> None:
    """
    Compare every matching file pair across two directories -- the
    real-world shape of a migration audit, where you're checking dozens
    of tables, not one. Files are matched by IDENTICAL FILENAME between
    source_dir and target_dir (e.g. source_dir/accounts.csv pairs with
    target_dir/accounts.csv) -- the same mental model as "same table,
    same name, different environment," and deliberately simple: no
    fuzzy filename matching, since guessing wrong at the FILE level
    (comparing the wrong two tables) is a much worse mistake than
    guessing wrong at the row-key level, which already has its own
    careful, opt-in fuzzy-matching path (--fuzzy-keys).

    Writes one report per pair into output_dir (named after the
    source file), plus a one-line summary per pair to the terminal,
    and a final tally. A failure on one pair (e.g. unrecognized file
    format, no detectable key) is reported and skipped -- it does NOT
    abort the whole batch, since the entire point of this command is
    surviving a large, messy real-world directory where a handful of
    files might not compare cleanly.

    Example:
        wherefore compare-dir old_exports/ new_exports/ --output-dir reports/
    """
    if explain_flag and not os.environ.get("ANTHROPIC_API_KEY"):
        typer.secho(
            "Error: --explain requires ANTHROPIC_API_KEY to be set in your environment.\n"
            'Run: export ANTHROPIC_API_KEY="sk-ant-..." before using --explain.',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    source_path = Path(source_dir)
    target_path = Path(target_dir)
    if not source_path.is_dir():
        typer.secho(f"Error: {source_dir!r} is not a directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if not target_path.is_dir():
        typer.secho(f"Error: {target_dir!r} is not a directory.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    pairs = _match_files_by_name(source_path, target_path)
    if not pairs:
        typer.secho(
            f"No matching filenames found between {source_dir!r} and {target_dir!r}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    typer.echo(f"Found {len(pairs)} matching file pair(s). Comparing...")
    typer.echo()

    succeeded = 0
    failed = 0
    all_redaction_categories: set[str] = set()

    for source_file, target_file in pairs:
        pair_label = source_file.name
        try:
            source_df = load_file(str(source_file))
            target_df = load_file(str(target_file))
        except (FileNotFoundError, ValueError, UnicodeDecodeError) as e:
            typer.secho(f"  [SKIPPED] {pair_label}: error loading files: {e}", fg=typer.colors.RED)
            failed += 1
            continue

        try:
            result = _run_comparison(
                source_df, target_df, key, fuzzy_keys, confidence_threshold, explain_flag, no_redact
            )
        except ValueError as e:
            typer.secho(f"  [SKIPPED] {pair_label}: {e}", fg=typer.colors.RED)
            failed += 1
            continue

        all_redaction_categories.update(result.redaction_categories)

        report = _render_report(
            str(source_file), str(target_file), result.join_column,
            result.diff_result, result.clusters, result.explanations,
        )
        report_path = output_path / f"{source_file.stem}_report.md"
        report_path.write_text(report)

        if not result.clusters:
            typer.secho(f"  [OK] {pair_label}: no mismatches", fg=typer.colors.GREEN)
        else:
            pattern_summary = ", ".join(
                p.pattern_id
                for c in result.clusters
                for p in c.candidate_patterns
            ) or "unrecognized pattern(s)"
            typer.secho(
                f"  [DIFF] {pair_label}: {len(result.clusters)} column(s) affected ({pattern_summary})",
                fg=typer.colors.CYAN,
            )
        succeeded += 1

    typer.echo()
    if all_redaction_categories:
        typer.secho(
            f"Redacted before sending to Claude (across all pairs): "
            f"{', '.join(sorted(all_redaction_categories))} -- pass --no-redact to disable this.",
            fg=typer.colors.YELLOW,
        )
    typer.secho(f"Done: {succeeded} compared, {failed} skipped. Reports written to {output_dir}/", fg=typer.colors.GREEN)


def _match_files_by_name(source_dir: Path, target_dir: Path) -> list[tuple[Path, Path]]:
    """
    Matches files between two directories by IDENTICAL FILENAME --
    deliberately simple, no fuzzy matching at the file level (see
    compare_dir's docstring for why). Only files present in BOTH
    directories are paired; files unique to one side are silently
    excluded from the pairing (not an error -- a real migration
    directory listing might legitimately have a new or removed table).
    Returns pairs sorted by filename for deterministic output ordering.
    """
    source_files = {p.name: p for p in source_dir.iterdir() if p.is_file()}
    target_files = {p.name: p for p in target_dir.iterdir() if p.is_file()}
    common_names = sorted(set(source_files) & set(target_files))
    return [(source_files[name], target_files[name]) for name in common_names]


def _auto_detect_key(source_df, target_df) -> str | None:
    """
    Picks a shared column that's (a) present in both files and (b) at
    least MIN_KEY_UNIQUENESS unique in both -- a reasonable proxy for
    "this looks like an identifier column," without requiring perfect
    uniqueness (real data sometimes has a handful of legitimate
    duplicate keys -- e.g. the dedup_failure scenario this tool also
    exists to catch -- so demanding 100% uniqueness would make
    auto-detect fail on exactly the kind of file this tool is for).
    Prefers columns whose name contains "id" or "key" when multiple
    candidates qualify, since that's a strong real-world naming
    convention; falls back to the first qualifying column otherwise.
    """
    shared_columns = [c for c in source_df.columns if c in target_df.columns]
    candidates = []
    for col in shared_columns:
        source_uniqueness = source_df[col].nunique() / max(len(source_df), 1)
        target_uniqueness = target_df[col].nunique() / max(len(target_df), 1)
        if source_uniqueness >= MIN_KEY_UNIQUENESS and target_uniqueness >= MIN_KEY_UNIQUENESS:
            candidates.append(col)

    if not candidates:
        return None

    id_like = [c for c in candidates if "id" in c.lower() or "key" in c.lower()]
    return id_like[0] if id_like else candidates[0]


def _apply_fuzzy_key_resolution(source_df, target_df, join_column):
    """
    Renames target rows' key values to their matched source key where
    a confident fuzzy match exists, so diff_engine's exact join then
    works correctly. Rows with unmatched or ambiguous keys are left
    as-is -- they'll show up as source-only/target-only rows in the
    diff, which is the honest outcome when a key genuinely couldn't be
    confidently resolved, rather than silently forcing a guess.
    """
    source_keys = source_df[join_column].astype(str).tolist()
    target_keys = target_df[join_column].astype(str).tolist()

    match_result = fuzzy_match_keys(source_keys, target_keys)

    if match_result.ambiguous_target_keys:
        preview = match_result.ambiguous_target_keys[:5]
        suffix = " ..." if len(match_result.ambiguous_target_keys) > 5 else ""
        typer.secho(
            f"Warning: {len(match_result.ambiguous_target_keys)} key(s) had ambiguous "
            f"fuzzy matches and were not auto-resolved: {preview}{suffix}",
            fg=typer.colors.YELLOW,
            err=True,
        )

    target_df = target_df.copy()
    target_df[join_column] = target_df[join_column].astype(str).map(
        lambda k: match_result.matched_pairs.get(k, k)
    )
    return source_df, target_df


def _render_report(source_path, target_path, join_column, diff_result, clusters, explanations: dict[str, ClusterExplanation] | None = None) -> str:
    explanations = explanations or {}
    lines = [
        "# wherefore comparison report",
        "",
        f"- Source: `{source_path}`",
        f"- Target: `{target_path}`",
        f"- Join key: `{join_column}`",
        f"- Source rows: {diff_result.source_row_count}",
        f"- Target rows: {diff_result.target_row_count}",
        f"- Matched rows: {diff_result.matched_row_count}",
        "",
    ]

    if explanations:
        lines += [
            "> **Note:** sections marked **AI explanation** below were generated",
            "> by calling the real Claude API (`--explain` was passed). Statistical",
            "> detail is shown alongside each one so you can verify the claim",
            "> against the actual evidence it was reasoned from.",
            "",
        ]
    else:
        lines += [
            "> **Note:** this report shows statistical findings only. Pass",
            "> `--explain` to additionally generate a plain-English causal",
            "> narrative for each cluster via the Claude API (requires",
            "> `ANTHROPIC_API_KEY` and makes real, billed API calls).",
            "",
        ]

    if diff_result.source_only_keys:
        lines.append(f"## Rows only in source ({len(diff_result.source_only_keys)})")
        lines.append("")
        for k in diff_result.source_only_keys[:20]:
            lines.append(f"- {k}")
        if len(diff_result.source_only_keys) > 20:
            lines.append(f"- ... and {len(diff_result.source_only_keys) - 20} more")
        lines.append("")

    if diff_result.target_only_keys:
        lines.append(f"## Rows only in target ({len(diff_result.target_only_keys)})")
        lines.append("")
        for k in diff_result.target_only_keys[:20]:
            lines.append(f"- {k}")
        if len(diff_result.target_only_keys) > 20:
            lines.append(f"- ... and {len(diff_result.target_only_keys) - 20} more")
        lines.append("")

    if not clusters:
        lines.append("## No mismatches found")
        lines.append("")
        lines.append("Every matched row compared identically across all columns.")
        return "\n".join(lines)

    lines.append(f"## Mismatches by column ({len(clusters)} column(s) affected)")
    lines.append("")

    for cluster in clusters:
        lines.append(f"### `{cluster.column}` -- {len(cluster.mismatches)} mismatched row(s)")
        lines.append("")

        explanation = explanations.get(cluster.column)
        if explanation is not None:
            lines.append(f"**AI explanation** (confidence: {explanation.confidence:.2f}):")
            lines.append("")
            lines.append(explanation.narrative)
            lines.append("")

        if cluster.is_unrecognized:
            lines.append("No known failure pattern's statistical signature matched this cluster.")
        else:
            for match in cluster.candidate_patterns:
                lines.append(
                    f"- Statistically matches **{match.pattern_id}** "
                    f"(signature: `{match.signature_name}`, confidence: {match.confidence:.2f})"
                )
        lines.append("")

        lines.append("Example rows:")
        lines.append("")
        if explanation is not None and explanation.cited_rows:
            for row in explanation.cited_rows:
                lines.append(f"- `{row.key}`: `{row.source_value}` -> `{row.target_value}` *(cited by AI)*")
        else:
            for m in cluster.mismatches[:5]:
                lines.append(f"- `{m.key}`: `{m.source_value}` -> `{m.target_value}`")
            if len(cluster.mismatches) > 5:
                lines.append(f"- ... and {len(cluster.mismatches) - 5} more")
        lines.append("")

    return "\n".join(lines)


def _print_summary(diff_result, clusters, output_path, explanations: dict[str, ClusterExplanation] | None = None) -> None:
    explanations = explanations or {}
    typer.echo(
        f"Compared {diff_result.source_row_count} source rows against "
        f"{diff_result.target_row_count} target rows."
    )
    typer.echo(f"Matched rows: {diff_result.matched_row_count}")
    if diff_result.source_only_keys:
        typer.echo(f"Rows only in source: {len(diff_result.source_only_keys)}")
    if diff_result.target_only_keys:
        typer.echo(f"Rows only in target: {len(diff_result.target_only_keys)}")

    if not clusters:
        typer.secho("No column mismatches found.", fg=typer.colors.GREEN)
    else:
        for cluster in clusters:
            if cluster.is_unrecognized:
                typer.echo(
                    f"  {cluster.column}: {len(cluster.mismatches)} mismatches, pattern unrecognized"
                )
            else:
                for match in cluster.candidate_patterns:
                    typer.secho(
                        f"  {cluster.column}: {len(cluster.mismatches)} mismatches, "
                        f"matches '{match.pattern_id}' (confidence {match.confidence:.2f})",
                        fg=typer.colors.CYAN,
                    )
            explanation = explanations.get(cluster.column)
            if explanation is not None:
                typer.secho(f"    AI: {explanation.narrative}", fg=typer.colors.MAGENTA)

    typer.secho(f"\nFull report written to {output_path}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
