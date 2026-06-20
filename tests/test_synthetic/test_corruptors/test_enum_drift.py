"""
Tests for synthetic/corruptors/enum_drift.py.
"""

import pandas as pd
import pytest

from wherefore.synthetic.base_dataset import HEALTHCARE_PATIENTS, generate_dataset
from wherefore.synthetic.corruptors.enum_drift import apply


@pytest.fixture
def healthcare_source():
    return generate_dataset(HEALTHCARE_PATIENTS, n_rows=50, seed=42)


def test_does_not_mutate_input_dataframe(healthcare_source):
    original = healthcare_source.copy(deep=True)
    apply(
        healthcare_source,
        column="claim_status",
        value_mapping={"approved": "APPROVED"},
        seed=1,
    )
    pd.testing.assert_frame_equal(healthcare_source, original)


def test_only_eligible_rows_are_affected(healthcare_source):
    """Only rows whose value is a KEY in value_mapping can be selected
    -- a row with 'submitted' or 'pending' (not in the mapping) must
    never be corrupted, regardless of selection."""
    mapping = {"approved": "APPROVED"}
    target, affected = apply(healthcare_source, column="claim_status", value_mapping=mapping, affected_fraction=1.0, seed=1)

    for idx in affected:
        assert healthcare_source.loc[idx, "claim_status"] == "approved"
        assert target.loc[idx, "claim_status"] == "APPROVED"


def test_mapping_is_consistent_for_every_affected_row(healthcare_source):
    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target, affected = apply(healthcare_source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=1)

    for idx in affected:
        original = healthcare_source.loc[idx, "claim_status"]
        new_value = target.loc[idx, "claim_status"]
        assert new_value == mapping[original]


def test_unaffected_rows_completely_untouched(healthcare_source):
    mapping = {"approved": "APPROVED"}
    target, affected = apply(healthcare_source, column="claim_status", value_mapping=mapping, affected_fraction=0.5, seed=1)
    unaffected = [i for i in range(len(healthcare_source)) if i not in affected]
    assert (
        target.loc[unaffected, "claim_status"] == healthcare_source.loc[unaffected, "claim_status"]
    ).all()


def test_other_columns_untouched(healthcare_source):
    mapping = {"approved": "APPROVED"}
    target, _ = apply(healthcare_source, column="claim_status", value_mapping=mapping, seed=1)
    for col in healthcare_source.columns:
        if col == "claim_status":
            continue
        pd.testing.assert_series_equal(target[col], healthcare_source[col])


def test_rejects_non_string_column(healthcare_source):
    with pytest.raises(TypeError, match="requires a string column"):
        apply(healthcare_source, column="billed_amount", value_mapping={"1": "2"})


def test_rejects_empty_value_mapping(healthcare_source):
    with pytest.raises(ValueError, match="value_mapping"):
        apply(healthcare_source, column="claim_status", value_mapping={})


def test_rejects_invalid_affected_fraction(healthcare_source):
    with pytest.raises(ValueError, match="affected_fraction"):
        apply(healthcare_source, column="claim_status", value_mapping={"approved": "X"}, affected_fraction=0.0)


def test_no_eligible_rows_returns_empty_affected_list(healthcare_source):
    """A mapping whose keys don't appear anywhere in the column should
    leave everything untouched and report zero affected rows -- not
    raise an error."""
    target, affected = apply(
        healthcare_source, column="claim_status", value_mapping={"not_a_real_status": "X"}, seed=1
    )
    assert affected == []
    pd.testing.assert_series_equal(target["claim_status"], healthcare_source["claim_status"])


def test_deterministic_given_same_seed(healthcare_source):
    mapping = {"approved": "APPROVED", "denied": "REJECTED"}
    target_a, affected_a = apply(healthcare_source, column="claim_status", value_mapping=mapping, seed=99)
    target_b, affected_b = apply(healthcare_source, column="claim_status", value_mapping=mapping, seed=99)
    assert affected_a == affected_b
    pd.testing.assert_frame_equal(target_a, target_b)
