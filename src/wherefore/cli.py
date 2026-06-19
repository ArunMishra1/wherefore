"""
cli.py

The `wherefore compare` command. Currently wires together everything
that's real: loaders -> (exact or fuzzy key resolution) ->
diff_engine -> cluster_mismatches -> a Markdown report of statistical
findings.

What this does NOT do yet: the AI reasoning layer doesn't exist, so
the report below shows WHAT was found (clusters, candidate pattern
matches, confidence scores) but not a plain-English causal narrative.
This is intentional, not a placeholder dressed up as a feature -- the
report says so explicitly (see _render_report's header) so nobody
mistakes "statistically matches timezone_shift at 1.00 confidence" for
an AI-written explanation of why it happened.
"""

from __future__ import annotations

from pathlib import Path

import typer

from wherefore.clustering.cluster_mismatches import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    cluster_mismatches,
)
from wherefore.comparison.diff_engine import compare as run_diff
from wherefore.comparison.key_matching import fuzzy_match_keys
from wherefore.comparison.loaders import load_file

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
) -> None:
    """
    Compare two datasets and show what's different, grouped by pattern
    where a statistical signature matches a known failure mode.

    Example:
        wherefore compare old_export.csv new_export.csv --output report.md
    """
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

    report = _render_report(source, target, join_column, diff_result, clusters)
    Path(output).write_text(report)

    _print_summary(diff_result, clusters, output)


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


def _render_report(source_path, target_path, join_column, diff_result, clusters) -> str:
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
        "> **Note:** this report shows statistical findings only. The AI",
        "> reasoning layer that writes plain-English causal explanations",
        "> isn't built yet -- see the project README for current status.",
        "> What you see below is what was deterministically measured, not",
        "> an explanation of why it happened.",
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
        for m in cluster.mismatches[:5]:
            lines.append(f"- `{m.key}`: `{m.source_value}` -> `{m.target_value}`")
        if len(cluster.mismatches) > 5:
            lines.append(f"- ... and {len(cluster.mismatches) - 5} more")
        lines.append("")

    return "\n".join(lines)


def _print_summary(diff_result, clusters, output_path) -> None:
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

    typer.secho(f"\nFull report written to {output_path}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
