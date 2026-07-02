"""
Tests for comparison/diff_engine.py. Each test below corresponds to a
scenario manually verified against real datacompy 1.0.2 output before
writing diff_engine.py -- see module docstrings in diff_result.py and
diff_engine.py for the design rationale resolved by that exploration
(particularly: dtype mismatches vs. value mismatches are tracked
independently, per ColumnSummary.dtype_mismatch).
"""

import pandas as pd
import pytest

from wherefore.comparison.diff_engine import compare
from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, generate_dataset
from wherefore.synthetic.corruptors.timezone_shift import apply


@pytest.fixture
def financial_source():
    return generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=42)


def test_identical_dataframes_produce_no_mismatches(financial_source):
    result = compare(financial_source, financial_source.copy(), join_columns="account_id")
    assert result.mismatches == []
    assert result.source_only_keys == []
    assert result.target_only_keys == []
    assert result.matched_row_count == len(financial_source)
    assert result.columns_with_mismatches() == []


def test_timezone_shift_produces_exact_expected_mismatches(financial_source):
    """
    End-to-end: corrupt a real fixture, diff it, confirm the diff
    engine reports exactly the rows the corruptor actually changed --
    not just "9 mismatches somewhere," but the SAME 9 keys.
    """
    target, affected_indices = apply(
        financial_source, column="opened_at", offset_hours=5.0, affected_fraction=0.3, seed=1
    )
    result = compare(financial_source, target, join_columns="account_id")

    assert result.columns_with_mismatches() == ["opened_at"]
    assert len(result.mismatches) == len(affected_indices)

    expected_keys = {financial_source.loc[i, "account_id"] for i in affected_indices}
    actual_keys = {m.key["account_id"] for m in result.mismatches}
    assert expected_keys == actual_keys

    # Every reported mismatch should show the exact 5-hour delta.
    for m in result.mismatches:
        assert (m.target_value - m.source_value) == pd.Timedelta(hours=5)


def test_other_columns_unaffected_by_timezone_shift_show_no_mismatches(financial_source):
    target, _ = apply(financial_source, column="opened_at", offset_hours=5.0, seed=1)
    result = compare(financial_source, target, join_columns="account_id")
    for col in ["customer_name", "account_type", "balance", "currency", "status"]:
        assert result.mismatches_for_column(col) == []


def test_source_only_and_target_only_rows_detected_by_key():
    source = pd.DataFrame({"id": [1, 2, 3], "val": [10, 20, 30]})
    target = pd.DataFrame({"id": [2, 3, 4], "val": [20, 30, 40]})

    result = compare(source, target, join_columns="id")
    assert result.source_only_keys == [{"id": 1}]
    assert result.target_only_keys == [{"id": 4}]
    assert result.matched_row_count == 2
    assert result.mismatches == []  # the 2 matched rows (id=2, id=3) are identical


def test_composite_join_keys():
    source = pd.DataFrame({"region": ["us", "us", "eu"], "id": [1, 2, 1], "val": [10, 20, 30]})
    target = pd.DataFrame({"region": ["us", "us", "eu"], "id": [1, 2, 1], "val": [10, 99, 30]})

    result = compare(source, target, join_columns=["region", "id"])
    assert len(result.mismatches) == 1
    mismatch = result.mismatches[0]
    assert mismatch.key == {"region": "us", "id": 2}
    assert mismatch.source_value == 20
    assert mismatch.target_value == 99


def test_dtype_mismatch_tracked_independently_of_value_mismatch():
    """
    Resolves the original open design question: a column with
    genuinely different dtypes on each side should report
    dtype_mismatch=True via ColumnSummary, distinct from per-row value
    mismatches in `mismatches`.
    """
    source = pd.DataFrame({"id": [1, 2, 3], "amount": [10.5, 20.5, 30.5]})
    target = pd.DataFrame({"id": [1, 2, 3], "amount": ["10.5", "20.5", "30.5"]})

    result = compare(source, target, join_columns="id")
    amount_summary = next(cs for cs in result.column_summary if cs.column == "amount")
    assert amount_summary.dtype_mismatch is True
    assert amount_summary.source_dtype != amount_summary.target_dtype
    assert len(result.mismatches) == 3  # datacompy does NOT silently coerce across dtypes


def test_dtype_mismatch_does_not_falsely_flag_identical_cells():
    """
    Regression test for a real bug caught while building
    null_type_coercion: datacompy's per-row {col}_match flag is
    UNRELIABLE for every row once a column's overall dtype differs
    between source and target -- confirmed directly that comparing
    [10.5, 20.5, 30.5] (float) against ['10.5', '20.5', '99.9'] (str)
    reports ALL THREE rows as mismatched via datacompy's own _match
    column, even though rows 1 and 2 have IDENTICAL values once you
    account for the type change being the point (a float becoming a
    string is itself the real, reportable finding -- but a row where
    BOTH the type and value are still genuinely identical on each side,
    e.g. the same pandas.Timestamp object appearing on both sides after
    an unrelated null in the same column forced it to object dtype,
    must not be reported as a mismatch just because the column's
    overall dtype changed).

    The fix compares (type, value) per cell rather than trusting
    datacompy's flag or comparing stringified representations -- string
    comparison was tried and rejected because it gets the `amount`
    case above WRONG (10.5 and '10.5' print identically but are a real
    type-change mismatch that must still be reported).
    """
    import pandas as pd

    # A column where most cells are genuinely identical Timestamps on
    # both sides, but ONE cell's source is null and gets coerced to a
    # string sentinel on the target -- this forces the WHOLE column to
    # object dtype, which is exactly what null_type_coercion produces.
    source = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "ts": pd.to_datetime(["2024-01-01", "2024-01-02", None]),
        }
    )
    target = source.copy()
    target["ts"] = target["ts"].astype(object)
    target.loc[2, "ts"] = "NULL"

    result = compare(source, target, join_columns="id")
    # Only row id=3 (the genuinely coerced null) should be a mismatch --
    # rows 1 and 2 have the identical Timestamp on both sides and must
    # NOT be falsely flagged just because the column's overall dtype changed.
    assert len(result.mismatches) == 1
    assert result.mismatches[0].key == {"id": 3}


def test_dtype_mismatch_with_genuinely_different_values_that_print_the_same():
    """
    The case that broke a naive string-comparison fix: a float and a
    string that happen to stringify identically (10.5 vs '10.5') is
    still a real type-change mismatch and must be reported, even
    though str(10.5) == '10.5'. This is the case that distinguishes
    "compare printed representation" (wrong) from "compare type AND
    value" (right) as the correct fix for the bug above.
    """
    source = pd.DataFrame({"id": [1, 2, 3], "val": [10.5, 20.5, 30.5]})
    target = pd.DataFrame({"id": [1, 2, 3], "val": ["10.5", "20.5", "99.9"]})

    result = compare(source, target, join_columns="id")
    # All three rows are a real mismatch: rows 1-2 changed TYPE (even
    # though the printed value looks the same), row 3 changed both
    # type and value.
    assert len(result.mismatches) == 3


def test_join_columns_excluded_from_column_summary(financial_source):
    """
    Join columns are equal by construction of the join -- they
    shouldn't appear in column_summary as if they were "compared".
    """
    result = compare(financial_source, financial_source.copy(), join_columns="account_id")
    summarized_columns = {cs.column for cs in result.column_summary}
    assert "account_id" not in summarized_columns


def test_string_join_column_normalized_to_list(financial_source):
    """compare() accepts a bare string for single-column joins."""
    result = compare(financial_source, financial_source.copy(), join_columns="account_id")
    assert result.join_columns == ["account_id"]


def test_target_only_rows_carries_full_row_content_not_just_keys():
    """
    The real regression test for the dedup_failure architectural
    extension: target_only_rows must carry every non-key column's
    value, not just the key -- confirmed this is what's actually
    needed to detect a row's content matches an existing row
    elsewhere, which target_only_keys (key-only) cannot support.
    """
    source = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"], "val": [10, 20]})
    target = pd.DataFrame({"id": [1, 2, 3], "name": ["Alice", "Bob", "Carol"], "val": [10, 20, 30]})

    result = compare(source, target, join_columns="id")
    assert len(result.target_only_rows) == 1
    record = result.target_only_rows[0]
    assert record.key == {"id": 3}
    assert record.values == {"name": "Carol", "val": 30}


def test_source_only_rows_carries_full_row_content():
    source = pd.DataFrame({"id": [1, 2, 3], "name": ["Alice", "Bob", "Carol"], "val": [10, 20, 30]})
    target = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"], "val": [10, 20]})

    result = compare(source, target, join_columns="id")
    assert len(result.source_only_rows) == 1
    record = result.source_only_rows[0]
    assert record.key == {"id": 3}
    assert record.values == {"name": "Carol", "val": 30}


def test_no_unique_rows_produces_empty_row_lists(financial_source):
    result = compare(financial_source, financial_source.copy(), join_columns="account_id")
    assert result.source_only_rows == []
    assert result.target_only_rows == []


def test_target_only_rows_and_keys_stay_consistent():
    """
    The key-only and full-row fields must agree on WHICH rows are
    unmatched -- they're two views of the same underlying data, not
    independent computations that could drift apart.
    """
    source = pd.DataFrame({"id": [1, 2], "val": [10, 20]})
    target = pd.DataFrame({"id": [1, 2, 3, 4], "val": [10, 20, 30, 40]})

    result = compare(source, target, join_columns="id")
    keys_from_key_field = {tuple(sorted(k.items())) for k in result.target_only_keys}
    keys_from_row_field = {tuple(sorted(r.key.items())) for r in result.target_only_rows}
    assert keys_from_key_field == keys_from_row_field


def test_default_key_match_strategy_is_exact(financial_source):
    """Every caller that doesn't pass fuzzy_match_confidence gets the
    same behavior as before this parameter was added -- key_match_strategy
    stays 'exact', and fuzzy_match_confidence stays None."""
    result = compare(financial_source, financial_source.copy(), join_columns="account_id")
    assert result.key_match_strategy == "exact"
    assert result.fuzzy_match_confidence is None


def test_fuzzy_match_confidence_is_passed_through_not_discarded():
    """
    compare() doesn't compute fuzzy match confidence itself (fuzzy
    resolution happens upstream, in key_matching.py, before the
    DataFrame reaches compare() -- see compare()'s docstring). This
    confirms it's faithfully threaded into the resulting DiffResult
    when a caller supplies it, rather than silently dropped.
    """
    source = pd.DataFrame({"id": ["A-1", "A-2"], "val": [10, 20]})
    target = pd.DataFrame({"id": ["A-1", "A-2"], "val": [10, 20]})

    confidence_map = {"A-1": 100.0, "A-2": 82.5}
    result = compare(source, target, join_columns="id", fuzzy_match_confidence=confidence_map)

    assert result.key_match_strategy == "fuzzy"
    assert result.fuzzy_match_confidence == confidence_map


def test_empty_fuzzy_match_confidence_dict_keeps_strategy_exact():
    """An empty dict is falsy -- passed through as 'no fuzzy matching
    actually happened' rather than spuriously flipping key_match_strategy
    to 'fuzzy' for a run that had nothing to report."""
    source = pd.DataFrame({"id": [1, 2], "val": [10, 20]})
    target = pd.DataFrame({"id": [1, 2], "val": [10, 20]})

    result = compare(source, target, join_columns="id", fuzzy_match_confidence={})
    assert result.key_match_strategy == "exact"


def test_matching_column_sets_produce_no_schema_drift():
    """The common case -- same columns on both sides -- must not
    spuriously report drift just because the columns happen to be
    supplied in different order (see
    test_column_order_does_not_affect_comparison below for why order
    specifically is a non-issue)."""
    source = pd.DataFrame({"id": [1, 2], "a": [1, 2], "b": [3, 4]})
    target = pd.DataFrame({"id": [1, 2], "b": [3, 4], "a": [1, 2]})

    result = compare(source, target, join_columns="id")
    assert result.source_only_columns == []
    assert result.target_only_columns == []
    assert result.has_schema_drift is False


def test_extra_source_column_is_reported_not_silently_dropped():
    """
    Before this, a column present only in source vanished with no
    trace anywhere in DiffResult -- datacompy's column_stats (and
    everything diff_engine.py builds from it) only ever covered the
    intersection. This locks in that the excluded column is now
    actually visible.
    """
    source = pd.DataFrame({"id": [1, 2], "a": [1, 2], "legacy_flag": ["Y", "N"]})
    target = pd.DataFrame({"id": [1, 2], "a": [1, 2]})

    result = compare(source, target, join_columns="id")
    assert result.source_only_columns == ["legacy_flag"]
    assert result.target_only_columns == []
    assert result.has_schema_drift is True
    # The excluded column must not leak into value-level comparison
    # output -- it was never joined against anything on the target
    # side, so it has nothing to be "compared" against.
    assert "legacy_flag" not in [c.column for c in result.column_summary]
    assert result.mismatches_for_column("legacy_flag") == []


def test_extra_target_column_is_reported():
    source = pd.DataFrame({"id": [1, 2], "a": [1, 2]})
    target = pd.DataFrame({"id": [1, 2], "a": [1, 2], "migrated_at": ["2024-01-01", "2024-01-02"]})

    result = compare(source, target, join_columns="id")
    assert result.source_only_columns == []
    assert result.target_only_columns == ["migrated_at"]
    assert result.has_schema_drift is True


def test_columns_only_on_both_sides_reported_independently():
    """Source dropped one column and gained another relative to
    target -- both directions must be reported, not just one, and a
    renamed column (no explicit mapping given) shows up as BOTH a
    source-only and a target-only entry, since wherefore doesn't
    guess at renames (see signatures.py's key_format_similarity
    docstring for why fuzzy name-matching was deliberately rejected
    for the analogous row-key case)."""
    source = pd.DataFrame({"id": [1, 2], "cust_id": [10, 20]})
    target = pd.DataFrame({"id": [1, 2], "customer_id": [10, 20]})

    result = compare(source, target, join_columns="id")
    assert result.source_only_columns == ["cust_id"]
    assert result.target_only_columns == ["customer_id"]


def test_column_order_does_not_affect_comparison():
    """
    Confirmed directly against real datacompy: matching is entirely
    name-keyed. Locks in that reordering columns on one side produces
    an IDENTICAL comparison result to the unreordered case -- same
    mismatches, same schema-drift result (none), regardless of
    position.
    """
    source = pd.DataFrame({"id": [1, 2, 3], "b": [4, 5, 6], "a": [1, 2, 3]})
    target = pd.DataFrame({"id": [1, 2, 3], "a": [1, 2, 30], "b": [4, 5, 6]})

    result = compare(source, target, join_columns="id")
    assert result.source_only_columns == []
    assert result.target_only_columns == []
    assert result.columns_with_mismatches() == ["a"]
    assert len(result.mismatches) == 1
    assert result.mismatches[0].key == {"id": 3}
