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

    join_column = key or _auto_detect_key(source_df, target_df)
    if join_column is None:
        typer.secho(
            "Could not auto-detect a join key column. "
            "Pass one explicitly with --key <column_name>.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if join_column not in source_df.columns or join_column not in target_df.columns:
        typer.secho(
            f"Key column {join_column!r} not found in both files.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=1)

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

        if all_redaction_categories:
            typer.secho(
                f"Redacted before sending to Claude: {', '.join(sorted(all_redaction_categories))} "
                f"-- pass --no-redact to disable this.",
                fg=typer.colors.YELLOW,
            )

    report = _render_report(source, target, join_column, diff_result, clusters, explanations)
    Path(output).write_text(report)

    _print_summary(diff_result, clusters, output, explanations)


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
