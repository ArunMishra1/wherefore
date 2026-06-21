"""
Tests for synthetic/corruptors/key_mismatch.py.
"""

import pandas as pd
import pytest

from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, generate_dataset
from wherefore.synthetic.corruptors.key_mismatch import apply


@pytest.fixture
def financial_source():
    return generate_dataset(FINANCIAL_ACCOUNTS, n_rows=30, seed=1)


def test_does_not_mutate_input_dataframe(financial_source):
    original = financial_source.copy(deep=True)
    apply(financial_source, key_column="account_id", seed=1)
    pd.testing.assert_frame_equal(financial_source, original)


def test_affected_rows_original_keys_no_longer_present_in_target(financial_source):
    target, original_keys = apply(financial_source, key_column="account_id", affected_fraction=0.2, seed=1)
    target_keys = set(target["account_id"])
    for k in original_keys:
        assert k not in target_keys


def test_affected_rows_reformatted_keys_are_present_in_target(financial_source):
    target, original_keys = apply(financial_source, key_column="account_id", affected_fraction=0.2, seed=1)
    target_keys = set(target["account_id"])
    for k in original_keys:
        reformatted = k.replace("-", "").replace("_", "")
        assert reformatted in target_keys


def test_row_content_unchanged_apart_from_key():
    df = pd.DataFrame({"id": ["A-1", "A-2", "A-3"], "name": ["a", "b", "c"], "val": [10, 20, 30]})
    target, original_keys = apply(df, key_column="id", affected_fraction=1.0, seed=1)

    for original_key in original_keys:
        original_row = df[df["id"] == original_key].iloc[0]
        reformatted_key = original_key.replace("-", "")
        target_row = target[target["id"] == reformatted_key].iloc[0]
        assert original_row["name"] == target_row["name"]
        assert original_row["val"] == target_row["val"]


def test_row_count_unchanged(financial_source):
    target, _ = apply(financial_source, key_column="account_id", affected_fraction=0.2, seed=1)
    assert len(target) == len(financial_source)


def test_rejects_missing_key_column(financial_source):
    with pytest.raises(ValueError, match="not found"):
        apply(financial_source, key_column="not_a_real_column")


def test_rejects_invalid_affected_fraction(financial_source):
    with pytest.raises(ValueError, match="affected_fraction"):
        apply(financial_source, key_column="account_id", affected_fraction=0.0)


def test_rejects_keys_with_no_separator_to_strip():
    """A key format with no dash/underscore would make this corruptor a
    silent no-op (the 'reformatted' key would be identical to the
    original) -- this must fail loudly instead of producing a fixture
    that doesn't actually test key_mismatch."""
    df = pd.DataFrame({"id": ["A1", "A2", "A3"], "val": [10, 20, 30]})
    with pytest.raises(ValueError, match="no '-' or '_'"):
        apply(df, key_column="id", affected_fraction=1.0, seed=1)


def test_deterministic_given_same_seed(financial_source):
    target_a, keys_a = apply(financial_source, key_column="account_id", seed=99)
    target_b, keys_b = apply(financial_source, key_column="account_id", seed=99)
    assert keys_a == keys_b
    pd.testing.assert_frame_equal(target_a, target_b)
