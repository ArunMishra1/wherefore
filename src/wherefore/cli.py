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
    detect_row_presence_patterns,
)
from wherefore.comparison.db import (
    _is_db_source,
    _table_name_from_db_source,
    connect as db_connect,
    detect_primary_key,
    list_tables,
    parse_connection_string,
    query_table,
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
    row_presence_clusters: list
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

    # key_mismatch (unresolved key-formatting drift) is detected via
    # detect_row_presence_patterns below, on diff_result.source_only_rows/
    # target_only_rows -- it doesn't need fuzzy_match_confidence at all.
    # fuzzy_match_confidence is threaded through to diff_result anyway
    # (rather than discarded, as it was before) because it's real signal
    # key_matching.py already computes and DiffResult already has a field
    # for -- a SEPARATE, not-yet-built detector (flagging fuzzy matches
    # that were accepted but only barely cleared the confidence floor)
    # would consume it later. No current caller reads diff_result.
    # fuzzy_match_confidence yet; that's an intentionally deferred next
    # step, not dead code -- see project notes on why that detector's
    # scoring needed more design work before shipping.
    fuzzy_match_confidence: dict[str, float] | None = None
    if fuzzy_keys:
        source_df, target_df, fuzzy_match_confidence = _apply_fuzzy_key_resolution(
            source_df, target_df, join_column
        )

    diff_result = run_diff(
        source_df, target_df, join_columns=join_column, fuzzy_match_confidence=fuzzy_match_confidence
    )
    clusters = cluster_mismatches(diff_result, confidence_threshold=confidence_threshold)
    row_presence_clusters = detect_row_presence_patterns(
        diff_result, source_df=source_df, target_df=target_df, confidence_threshold=confidence_threshold
    )

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
        row_presence_clusters=row_presence_clusters,
        explanations=explanations,
        redaction_categories=all_redaction_categories,
    )


def _load_source(
    source: str,
    conn_env_var: str | None,
    side_label: str,
):
    """
    The single dispatch point compare() calls instead of load_file()
    directly -- mirrors loaders.py's own internal dispatch discipline
    (check the special case BEFORE doing anything path-like) one level
    up, at the CLI layer, since db:// is a CLI-only source syntax, not
    a file format load_file() should know about.

    For a db:// source: resolves `conn_env_var` (an ENVIRONMENT
    VARIABLE NAME, never a literal connection string -- see db.py's
    module docstring for why) to its real value, parses it, connects,
    and queries the named table. Primary-key detection/confirmation is
    NOT done here -- see _detect_db_primary_keys/_confirm_db_primary_key,
    called separately from compare() before this, since the
    confirmation decision needs visibility into BOTH sides' detected
    keys at once (showing one side's prompt, getting a "yes", then
    discovering the other side disagrees, would be a worse user
    experience than one combined prompt).

    For anything else: unchanged behavior, delegates to load_file().

    `side_label` ("source" or "target") is only used to make error
    messages specific about which of the two inputs failed --
    cosmetic, not behavioral.
    """
    if not _is_db_source(source):
        return load_file(source)

    if not conn_env_var:
        raise ValueError(
            f"{source!r} uses the db:// syntax but no connection-string environment "
            f"variable was given for the {side_label} side. Pass "
            f"--{side_label}-conn-env YOUR_ENV_VAR_NAME, where YOUR_ENV_VAR_NAME is "
            "set to a real connection string (e.g. export "
            'YOUR_ENV_VAR_NAME="sqlite:////absolute/path/to/file.sqlite").'
        )

    conn_str = os.environ.get(conn_env_var)
    if conn_str is None:
        raise ValueError(
            f"Environment variable {conn_env_var!r} is not set. It should hold a "
            "real database connection string, e.g. "
            f'export {conn_env_var}="sqlite:////absolute/path/to/file.sqlite". '
            "wherefore reads the connection string from an env var, never from the "
            "command line itself, so credentials never end up in argv or shell history."
        )

    table_name = _table_name_from_db_source(source)
    info = parse_connection_string(conn_str)
    conn = db_connect(info)
    return query_table(conn, table_name)


def _detect_db_primary_keys(
    source: str, target: str, source_conn_env: str | None, target_conn_env: str | None
) -> dict[str, list[str] | None]:
    """
    For each side that's a db:// source, connects and reads the
    database's own schema metadata to find its real primary key --
    NOT the file-based heuristic _auto_detect_key uses (uniqueness
    ratio, "id"/"key" in the column name). Returns a dict keyed by
    side_label ("source"/"target") to whatever was found, omitting
    sides that aren't db:// sources at all (nothing to detect for a
    plain file).

    Deliberately separate from _load_source: this function ONLY
    detects and reports, it does not decide whether to proceed --
    that decision belongs to _confirm_db_primary_key, called from
    compare() after this, so the user sees BOTH sides' detected keys
    (or lack thereof) in one combined prompt before anything runs.
    """
    detected: dict[str, list[str] | None] = {}
    for side_label, src, conn_env in (
        ("source", source, source_conn_env),
        ("target", target, target_conn_env),
    ):
        if not _is_db_source(src) or not conn_env:
            continue
        conn_str = os.environ.get(conn_env)
        if conn_str is None:
            continue  # _load_source will raise its own clear error for this later
        table_name = _table_name_from_db_source(src)
        info = parse_connection_string(conn_str)
        conn = db_connect(info)
        detected[side_label] = detect_primary_key(conn, table_name)
    return detected


def _confirm_db_primary_key(detected: dict[str, list[str] | None], assume_yes: bool) -> bool:
    """
    Shows the user exactly what primary key(s) were auto-detected from
    real database schema metadata for each db:// source involved, and
    requires explicit confirmation before the comparison proceeds --
    per the roadmap's own stated reasoning: a wrong auto-detected key
    against a REAL, possibly production database is a materially more
    serious mistake than a wrong key on a CSV (a CSV mistake produces
    a wrong report; a database mistake means a query ran against a
    live system based on a guess nobody reviewed). This is why this
    confirmation step exists for db:// sources but NOT for files, even
    though both have an auto-detect path -- the stakes are genuinely
    different, not just stylistically inconsistent.

    Returns True if the user confirmed (or --yes was passed), False if
    they declined -- compare() is responsible for exiting cleanly on
    False, this function never calls typer.Exit itself, since "what
    happens on decline" is a caller decision (e.g. compare_dir, if/when
    db:// support is added there, might want to skip-and-continue
    rather than abort the whole batch the way compare() does).

    If `detected` is empty (neither side is a db:// source with a
    resolved connection), there's nothing to confirm -- returns True
    immediately without printing anything, so file-only comparisons
    are completely unaffected by this function existing.
    """
    if not detected:
        return True

    typer.echo()
    for side_label, pk_columns in detected.items():
        if pk_columns is None:
            typer.secho(
                f"  {side_label}: no primary key found in the database schema for this table.",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.secho(
                f"  {side_label}: detected primary key {', '.join(pk_columns)!r} from the database schema.",
                fg=typer.colors.CYAN,
            )

    if assume_yes:
        typer.echo("(--yes passed, proceeding without interactive confirmation)")
        return True

    return typer.confirm("Proceed with this key?", default=False)


@app.command()
def compare(
    source: str = typer.Argument(
        ..., help="Path to the source file (CSV/JSON/Parquet/Excel, local or s3://), or db://table_name."
    ),
    target: str = typer.Argument(
        ..., help="Path to the target file (CSV/JSON/Parquet/Excel, local or s3://), or db://table_name."
    ),
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
    source_conn_env: str = typer.Option(
        None,
        "--source-conn-env",
        help="Required if SOURCE uses db://table_name: the NAME of an environment variable "
        "holding the real connection string (e.g. --source-conn-env SOURCE_DB, with "
        'export SOURCE_DB="sqlite:////absolute/path/to/file.sqlite" set separately). '
        "The connection string itself is never accepted on the command line, so it never "
        "ends up in argv or shell history.",
    ),
    target_conn_env: str = typer.Option(
        None,
        "--target-conn-env",
        help="Same as --source-conn-env, for TARGET.",
    ),
    assume_yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt for an auto-detected database primary "
        "key (db:// sources only; has no effect on file-based comparisons). Use with care -- "
        "this is the one safety check standing between a wrong guess and a query running "
        "against a real database unreviewed.",
    ),
) -> None:
    """
    Compare two datasets and show what's different, grouped by pattern
    where a statistical signature matches a known failure mode.

    Example:
        wherefore compare old_export.csv new_export.csv --output report.md
        wherefore compare old_export.csv new_export.csv --explain
        export SOURCE_DB="sqlite:////absolute/path/to/old.sqlite"
        export TARGET_DB="sqlite:////absolute/path/to/new.sqlite"
        wherefore compare db://accounts db://accounts \\
            --source-conn-env SOURCE_DB --target-conn-env TARGET_DB
    """
    if explain_flag and not os.environ.get("ANTHROPIC_API_KEY"):
        typer.secho(
            "Error: --explain requires ANTHROPIC_API_KEY to be set in your environment.\n"
            'Run: export ANTHROPIC_API_KEY="sk-ant-..." before using --explain.',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if key is None:
        try:
            detected_keys = _detect_db_primary_keys(source, target, source_conn_env, target_conn_env)
        except (ValueError, FileNotFoundError, NotImplementedError) as e:
            typer.secho(f"Error connecting to database: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

        if detected_keys:
            if not _confirm_db_primary_key(detected_keys, assume_yes):
                typer.secho("Aborted.", fg=typer.colors.YELLOW)
                raise typer.Exit(code=1)
            # A detected single-column key becomes the explicit --key
            # for the rest of this run, exactly as if the user had
            # passed it themselves -- this is the ONLY path by which a
            # database's auto-detected key is actually used, and only
            # after the confirmation above succeeded. A composite key
            # (more than one column) isn't reducible to the single
            # `key` string the rest of the pipeline expects yet --
            # that's real, tracked future work (join_columns support
            # for db:// sources specifically), not silently mishandled:
            # it's reported in the error below rather than guessed at.
            single_column_keys = {
                side: cols for side, cols in detected_keys.items() if cols and len(cols) == 1
            }
            if single_column_keys:
                key = next(iter(single_column_keys.values()))[0]
            elif any(cols and len(cols) > 1 for cols in detected_keys.values()):
                typer.secho(
                    "Error: a composite (multi-column) primary key was detected, but "
                    "wherefore's --key currently only supports a single column for db:// "
                    "sources. Pass --key explicitly with one column name to proceed "
                    "(this will compare using that single column as the join key, not "
                    "the full composite key).",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)

    try:
        source_df = _load_source(source, source_conn_env, "source")
        target_df = _load_source(target, target_conn_env, "target")
    except (FileNotFoundError, ValueError, UnicodeDecodeError, RuntimeError, ImportError, NotImplementedError) as e:
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
        source, target, result.join_column, result.diff_result, result.clusters, result.explanations,
        row_presence_clusters=result.row_presence_clusters,
    )
    Path(output).write_text(report)

    _print_summary(
        result.diff_result, result.clusters, output, result.explanations,
        row_presence_clusters=result.row_presence_clusters,
    )


@app.command(name="compare-dir")
def compare_dir(
    source_dir: str = typer.Argument(
        ..., help="Directory of source files, or db://* to compare every table in a database."
    ),
    target_dir: str = typer.Argument(
        ..., help="Directory of target files with matching filenames, or db://* for the target database."
    ),
    output_dir: str = typer.Option(
        "reports", "--output-dir", help="Directory to write one report per matched pair."
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
    source_conn_env: str = typer.Option(
        None,
        "--source-conn-env",
        help="Required if SOURCE_DIR is db://*: the NAME of an environment variable holding the real "
        "connection string. Never the connection string itself.",
    ),
    target_conn_env: str = typer.Option(
        None,
        "--target-conn-env",
        help="Same as --source-conn-env, for TARGET_DIR.",
    ),
    assume_yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt for auto-detected database primary keys "
        "(db:// batch mode only; has no effect on file-based directories).",
    ),
) -> None:
    """
    Compare every matching pair across two sources -- the real-world
    shape of a migration audit, where you're checking dozens of
    tables, not one. Two source-pair shapes are supported, matched the
    SAME way on both sides (mixing a directory with a database is not
    supported -- see below for why):

    Two directories: files matched by IDENTICAL FILENAME (e.g.
    source_dir/accounts.csv pairs with target_dir/accounts.csv) -- the
    same mental model as "same table, same name, different
    environment," and deliberately simple: no fuzzy filename matching,
    since guessing wrong at the FILE level (comparing the wrong two
    tables) is a much worse mistake than guessing wrong at the
    row-key level, which already has its own careful, opt-in
    fuzzy-matching path (--fuzzy-keys).

    Two databases (db://* on both sides -- ANY db://... string works,
    since whatever follows db:// is ignored in this mode; db://* is
    just the documented convention for "I mean every table," not a
    literal requirement enforced anywhere): every real, user-created
    table present in BOTH databases is compared, matched by identical
    table name -- the same "match by name, exclude what's only on one
    side" principle as the directory case, just reading the table list
    from each database's own schema instead of a directory listing.
    Requires --source-conn-env/--target-conn-env (see compare's own
    docstring for why the connection string itself is never a CLI
    argument). Every detected primary key is shown ONCE, as a combined
    list covering the whole batch, with a single confirmation prompt
    -- not one prompt per table, which would defeat the purpose of
    batch mode, but also not silent, which would defeat the purpose of
    the confirmation existing at all. A table with no detectable
    primary key and no explicit --key is skipped individually (same
    "skip, don't abort the batch" principle the file-based mode
    already uses for an unrecognized file format), not treated as a
    reason to stop everything.

    Mixing a directory with a database (one side db://, the other a
    file path) is explicitly rejected with a clear error rather than
    guessed at -- pairing a table name against a filename isn't
    symmetric the way two directories or two databases are (e.g.
    should "accounts.csv" match table "accounts"? what about
    "accounts_2024.csv"?), and this needs its own real design pass,
    not a guess bolted onto this command's two already-working modes.

    Writes one report per pair into output_dir (named after the
    source file's stem, or the table name for database mode), plus a
    one-line summary per pair to the terminal, and a final tally. A
    failure on one pair (e.g. unrecognized file format, no detectable
    key) is reported and skipped -- it does NOT abort the whole batch,
    since the entire point of this command is surviving a large,
    messy real-world source where a handful of pairs might not compare
    cleanly.

    Example:
        wherefore compare-dir old_exports/ new_exports/ --output-dir reports/
        export SOURCE_DB="postgresql://user:pass@host/old_db"
        export TARGET_DB="postgresql://user:pass@host/new_db"
        wherefore compare-dir db://* db://* --source-conn-env SOURCE_DB --target-conn-env TARGET_DB
    """
    if explain_flag and not os.environ.get("ANTHROPIC_API_KEY"):
        typer.secho(
            "Error: --explain requires ANTHROPIC_API_KEY to be set in your environment.\n"
            'Run: export ANTHROPIC_API_KEY="sk-ant-..." before using --explain.',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    source_is_db = _is_db_source(source_dir)
    target_is_db = _is_db_source(target_dir)

    if source_is_db != target_is_db:
        typer.secho(
            "Error: mixing a database (db://*) with a directory of files is not supported for "
            "compare-dir. Both SOURCE_DIR and TARGET_DIR must be the same kind (both directories, "
            "or both db://*) -- pairing a table name against a filename isn't a well-defined "
            "matching rule yet (this is real, tracked future work, not a silent gap).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if source_is_db:
        pairs, per_pair_key = _prepare_db_dir_pairs(
            source_dir, target_dir, source_conn_env, target_conn_env, key, assume_yes
        )
    else:
        source_path = Path(source_dir)
        target_path = Path(target_dir)
        if not source_path.is_dir():
            typer.secho(f"Error: {source_dir!r} is not a directory.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        if not target_path.is_dir():
            typer.secho(f"Error: {target_dir!r} is not a directory.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

        file_pairs = _match_files_by_name(source_path, target_path)
        if not file_pairs:
            typer.secho(
                f"No matching filenames found between {source_dir!r} and {target_dir!r}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        # pair_label is the full filename (e.g. "accounts.csv") for
        # terminal display, matching this command's original behavior
        # -- NOT the same as the report filename, which uses just the
        # stem (e.g. "accounts_report.md", no ".csv" in the middle).
        # report_stem is tracked separately so fixing this distinction
        # doesn't require re-deriving it from pair_label later via
        # string splitting.
        pairs = [
            (str(src), str(tgt), src.name, src.stem) for src, tgt in file_pairs
        ]
        per_pair_key = {label: key for _, _, label, _ in pairs}

    noun = "table" if source_is_db else "file"
    typer.echo(f"Found {len(pairs)} matching {noun} pair(s). Comparing...")
    typer.echo()

    succeeded = 0
    failed = 0
    all_redaction_categories: set[str] = set()

    for source_ref, target_ref, pair_label, report_stem in pairs:
        try:
            if source_is_db:
                source_df = _load_source(source_ref, source_conn_env, "source")
                target_df = _load_source(target_ref, target_conn_env, "target")
            else:
                source_df = load_file(source_ref)
                target_df = load_file(target_ref)
        except (FileNotFoundError, ValueError, UnicodeDecodeError, RuntimeError, ImportError, NotImplementedError) as e:
            typer.secho(f"  [SKIPPED] {pair_label}: error loading data: {e}", fg=typer.colors.RED)
            failed += 1
            continue

        pair_key = per_pair_key.get(pair_label)
        if source_is_db and pair_key is None:
            typer.secho(
                f"  [SKIPPED] {pair_label}: no usable primary key (no single-column key detected, "
                "and no --key given).",
                fg=typer.colors.RED,
            )
            failed += 1
            continue

        try:
            result = _run_comparison(
                source_df, target_df, pair_key, fuzzy_keys, confidence_threshold, explain_flag, no_redact
            )
        except ValueError as e:
            typer.secho(f"  [SKIPPED] {pair_label}: {e}", fg=typer.colors.RED)
            failed += 1
            continue

        all_redaction_categories.update(result.redaction_categories)

        report = _render_report(
            source_ref, target_ref, result.join_column,
            result.diff_result, result.clusters, result.explanations,
            row_presence_clusters=result.row_presence_clusters,
        )
        report_path = output_path / f"{report_stem}_report.md"
        report_path.write_text(report)

        total_findings = len(result.clusters) + len(result.row_presence_clusters)
        if total_findings == 0:
            typer.secho(f"  [OK] {pair_label}: no mismatches", fg=typer.colors.GREEN)
        else:
            pattern_names = [p.pattern_id for c in result.clusters for p in c.candidate_patterns]
            pattern_names += [p.pattern_id for c in result.row_presence_clusters for p in c.candidate_patterns]
            pattern_summary = ", ".join(pattern_names) or "unrecognized pattern(s)"
            typer.secho(
                f"  [DIFF] {pair_label}: {total_findings} finding(s) ({pattern_summary})",
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


def _prepare_db_dir_pairs(
    source_dir: str,
    target_dir: str,
    source_conn_env: str | None,
    target_conn_env: str | None,
    explicit_key: str | None,
    assume_yes: bool,
) -> tuple[list[tuple[str, str, str, str]], dict[str, str | None]]:
    """
    The database-batch-mode equivalent of _match_files_by_name --
    connects to both databases, lists every real table on each side
    (db.py's list_tables), and intersects by name, the same "match by
    identical name, exclude what's only on one side" principle as the
    file-based version (see compare_dir's docstring).

    UNLIKE the file-based path, this also handles primary-key
    detection/confirmation for the WHOLE BATCH up front, in one
    combined prompt -- not because the underlying mechanism differs
    from compare()'s single-table _detect_db_primary_keys/
    _confirm_db_primary_key (it's the same schema introspection), but
    because showing N separate per-table prompts in a batch of,
    say, 20 tables would defeat the entire purpose of batch mode,
    while skipping confirmation silently would defeat the entire
    purpose of the confirmation existing at all (see
    _confirm_db_primary_key's own docstring for why this safety check
    exists specifically for databases, not files).

    Returns (pairs, per_pair_key):
    - pairs: list of (source_ref, target_ref, pair_label, report_stem)
      tuples, where source_ref/target_ref are real db://table_name
      strings ready to pass straight into _load_source. pair_label and
      report_stem are identical here (both just the bare table name)
      -- the two-value shape exists to match the file-based branch's
      tuple shape exactly, where pair_label (full filename) and
      report_stem (filename without extension) genuinely differ.
    - per_pair_key: maps each pair_label to the join key that should
      be used for that table -- the explicit --key if one was given
      (applied to every table, matching the file-based mode's own
      "applied to every pair" semantics), otherwise that table's own
      detected single-column primary key, or None if no usable key
      was found (the caller skips that one table, not the whole
      batch -- same "skip, don't abort" principle as an unrecognized
      file format).

    Composite (multi-column) primary keys are reported in the
    confirmation listing but, same as compare()'s single-table path,
    are not yet usable as an actual join key -- that table's
    per_pair_key entry is None, so it gets skipped individually with a
    clear reason rather than guessed at.

    Exits the whole command (not just one pair) if the user declines
    the combined confirmation -- unlike a per-table loading error,
    declining a confirmation that covers the WHOLE batch is a single
    yes/no decision about whether to proceed at all, not a per-pair
    outcome to skip past.
    """
    if not source_conn_env or not target_conn_env:
        typer.secho(
            "Error: db://* batch mode requires both --source-conn-env and --target-conn-env "
            "(the NAME of an environment variable holding a real connection string, never the "
            "connection string itself).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        source_conn_str = os.environ[source_conn_env]
        target_conn_str = os.environ[target_conn_env]
    except KeyError as e:
        typer.secho(
            f"Error: environment variable {e.args[0]!r} is not set. It should hold a real "
            "database connection string.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        source_conn = db_connect(parse_connection_string(source_conn_str))
        target_conn = db_connect(parse_connection_string(target_conn_str))
        source_tables = set(list_tables(source_conn))
        target_tables = set(list_tables(target_conn))
    except (ValueError, FileNotFoundError, NotImplementedError, RuntimeError) as e:
        typer.secho(f"Error connecting to database: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    common_tables = sorted(source_tables & target_tables)
    if not common_tables:
        typer.secho(
            "No matching table names found between the source and target databases.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    per_pair_key: dict[str, str | None] = {}
    if explicit_key:
        # --key applies to every table, matching the file-based mode's
        # own "applied to every pair" semantics -- skip detection
        # entirely, same as compare()'s single-table path does.
        for table in common_tables:
            per_pair_key[table] = explicit_key
    else:
        typer.echo()
        typer.echo(f"Detecting primary keys for {len(common_tables)} table(s)...")
        for table in common_tables:
            try:
                source_pk = detect_primary_key(source_conn, table)
                target_pk = detect_primary_key(target_conn, table)
            except ValueError as e:
                typer.secho(f"  {table}: error reading schema: {e}", fg=typer.colors.RED)
                per_pair_key[table] = None
                continue

            # Both sides must agree on a usable single-column key --
            # if either side has none, or either is composite, this
            # table can't get an auto-applied key (reported, not
            # guessed at -- see this function's docstring).
            usable = (
                source_pk is not None and len(source_pk) == 1
                and target_pk is not None and len(target_pk) == 1
            )
            if source_pk is None or target_pk is None:
                typer.secho(f"  {table}: no primary key found on at least one side.", fg=typer.colors.YELLOW)
            elif len(source_pk) > 1 or len(target_pk) > 1:
                typer.secho(
                    f"  {table}: composite primary key detected ({', '.join(source_pk)}) -- "
                    "not yet usable as a join key in batch mode.",
                    fg=typer.colors.YELLOW,
                )
            else:
                typer.secho(f"  {table}: detected primary key {source_pk[0]!r}.", fg=typer.colors.CYAN)

            per_pair_key[table] = source_pk[0] if usable else None

        typer.echo()
        if assume_yes:
            typer.echo("(--yes passed, proceeding without interactive confirmation)")
        elif not typer.confirm(
            f"Proceed comparing {len(common_tables)} table(s) with the keys shown above?", default=False
        ):
            typer.secho("Aborted.", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1)

    pairs = [
        (f"db://{table}", f"db://{table}", table, table) for table in common_tables
    ]
    return pairs, per_pair_key


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

    Also returns match_result.confidence_by_target_key (keyed by the
    ORIGINAL, pre-rename target key) so the caller can pass it through
    to diff_engine.compare() instead of discarding it -- a fuzzy match
    that was accepted but scored low is exactly the key_mismatch
    taxonomy pattern's signal (see key_matching.py's module docstring),
    and it's only visible here, before the rename makes the key look
    like a clean exact match to everything downstream.
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
    return source_df, target_df, match_result.confidence_by_target_key


def _render_report(
    source_path,
    target_path,
    join_column,
    diff_result,
    clusters,
    explanations: dict[str, ClusterExplanation] | None = None,
    row_presence_clusters: list | None = None,
) -> str:
    explanations = explanations or {}
    row_presence_clusters = row_presence_clusters or []
    row_presence_by_side = {c.side: c for c in row_presence_clusters}
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
        source_only_match = row_presence_by_side.get("source_only")
        if source_only_match and not source_only_match.is_unrecognized:
            for p in source_only_match.candidate_patterns:
                lines.append(
                    f"- Statistically matches **{p.pattern_id}** "
                    f"(signature: `{p.signature_name}`, confidence: {p.confidence:.2f})"
                )
            lines.append("")
        for k in diff_result.source_only_keys[:20]:
            lines.append(f"- {k}")
        if len(diff_result.source_only_keys) > 20:
            lines.append(f"- ... and {len(diff_result.source_only_keys) - 20} more")
        lines.append("")

    if diff_result.target_only_keys:
        lines.append(f"## Rows only in target ({len(diff_result.target_only_keys)})")
        lines.append("")
        target_only_match = row_presence_by_side.get("target_only")
        if target_only_match and not target_only_match.is_unrecognized:
            for p in target_only_match.candidate_patterns:
                lines.append(
                    f"- Statistically matches **{p.pattern_id}** "
                    f"(signature: `{p.signature_name}`, confidence: {p.confidence:.2f})"
                )
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


def _print_summary(
    diff_result,
    clusters,
    output_path,
    explanations: dict[str, ClusterExplanation] | None = None,
    row_presence_clusters: list | None = None,
) -> None:
    explanations = explanations or {}
    row_presence_by_side = {c.side: c for c in (row_presence_clusters or [])}
    typer.echo(
        f"Compared {diff_result.source_row_count} source rows against "
        f"{diff_result.target_row_count} target rows."
    )
    typer.echo(f"Matched rows: {diff_result.matched_row_count}")

    if diff_result.source_only_keys:
        typer.echo(f"Rows only in source: {len(diff_result.source_only_keys)}")
        _print_row_presence_match(row_presence_by_side.get("source_only"))
    if diff_result.target_only_keys:
        typer.echo(f"Rows only in target: {len(diff_result.target_only_keys)}")
        _print_row_presence_match(row_presence_by_side.get("target_only"))

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


def _print_row_presence_match(row_presence_cluster) -> None:
    if row_presence_cluster is None or row_presence_cluster.is_unrecognized:
        return
    for match in row_presence_cluster.candidate_patterns:
        typer.secho(
            f"  matches '{match.pattern_id}' (confidence {match.confidence:.2f})",
            fg=typer.colors.CYAN,
        )


if __name__ == "__main__":
    app()
