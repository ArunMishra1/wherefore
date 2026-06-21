"""
Tests for synthetic/corruptors/encoding_mismatch.py.
"""

import pandas as pd
import pytest

from wherefore.synthetic.base_dataset import FINANCIAL_ACCOUNTS, generate_dataset
from wherefore.synthetic.corruptors.encoding_mismatch import apply


@pytest.fixture
def financial_source():
    return generate_dataset(FINANCIAL_ACCOUNTS, n_rows=50, seed=1)


def test_does_not_mutate_input_dataframe(financial_source):
    original = financial_source.copy(deep=True)
    apply(financial_source, column="customer_name", seed=1)
    pd.testing.assert_frame_equal(financial_source, original)


def test_affected_rows_are_exact_utf8_to_latin1_mojibake(financial_source):
    target, affected = apply(financial_source, column="customer_name", affected_fraction=1.0, seed=1)
    assert len(affected) > 0
    for idx in affected:
        original = str(financial_source.loc[idx, "customer_name"])
        corrupted = target.loc[idx, "customer_name"]
        assert corrupted == original.encode("utf-8").decode("latin-1")
        assert corrupted != original


def test_pure_ascii_names_are_not_reported_as_affected():
    df = pd.DataFrame({"id": [1], "name": ["Susan Miller"]})
    target, affected = apply(df, column="name", affected_fraction=1.0, seed=1)
    assert affected == []
    assert target.loc[0, "name"] == "Susan Miller"


def test_real_mojibake_example_matches_known_output():
    df = pd.DataFrame({"id": [1], "name": ["José"]})
    target, affected = apply(df, column="name", affected_fraction=1.0, seed=1)
    assert target.loc[0, "name"] == "JosÃ©"


def test_unaffected_rows_completely_untouched(financial_source):
    target, affected = apply(financial_source, column="customer_name", affected_fraction=0.5, seed=1)
    unaffected = [i for i in range(len(financial_source)) if i not in affected]
    assert (
        target.loc[unaffected, "customer_name"] == financial_source.loc[unaffected, "customer_name"]
    ).all()


def test_other_columns_untouched(financial_source):
    target, _ = apply(financial_source, column="customer_name", seed=1)
    for col in financial_source.columns:
        if col == "customer_name":
            continue
        pd.testing.assert_series_equal(target[col], financial_source[col])


def test_rejects_non_string_column(financial_source):
    with pytest.raises(TypeError, match="requires a string column"):
        apply(financial_source, column="balance")


def test_rejects_invalid_affected_fraction(financial_source):
    with pytest.raises(ValueError, match="affected_fraction"):
        apply(financial_source, column="customer_name", affected_fraction=0.0)


def test_deterministic_given_same_seed(financial_source):
    target_a, affected_a = apply(financial_source, column="customer_name", seed=99)
    target_b, affected_b = apply(financial_source, column="customer_name", seed=99)
    assert affected_a == affected_b
    pd.testing.assert_frame_equal(target_a, target_b)


def test_mojibake_is_deterministically_reversible():
    df = pd.DataFrame({"id": [1], "name": ["Müller"]})
    target, _ = apply(df, column="name", affected_fraction=1.0, seed=1)
    mojibake = target.loc[0, "name"]
    recovered = mojibake.encode("latin-1").decode("utf-8")
    assert recovered == "Müller"
