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
