"""
cli.py

NEXT TURN: implement this.

Purpose: Typer app exposing the spec's target command:

    wherefore compare source.csv target.csv --output report.md

Planned flow:
    1. loaders.load(source_path), loaders.load(target_path)
    2. key_matching.resolve_keys(source_df, target_df) -- exact or fuzzy
    3. diff_engine.compare(source_df, target_df, keys) -> DiffResult
    4. cluster_mismatches.cluster(diff_result) -> list[MismatchCluster]
    5. for each cluster: reasoning.explain.explain(cluster) -> ClusterExplanation
    6. reasoning.report.render(explanations, metadata) -> Markdown string
    7. write to --output path

Also planned: --verbose flag to show detection_hint statistical
observations even for unmatched clusters (useful for debugging/trust-
building -- lets a skeptical user see the deterministic evidence, not
just trust the LLM's narrative blindly), and a way to run without an
API key (--no-llm?) that produces a report with statistical
observations only, for users who want to evaluate the comparison
engine before committing to API costs.
"""

import typer

app = typer.Typer()


@app.command()
def compare(
    source: str,
    target: str,
    output: str = "report.md",
) -> None:
    """Compare two datasets and explain WHY they differ, in plain English."""
    raise NotImplementedError("Scaffold only -- see module docstring for planned flow.")


if __name__ == "__main__":
    app()
