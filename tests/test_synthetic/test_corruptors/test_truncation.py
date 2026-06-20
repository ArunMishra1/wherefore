"""
Tests for synthetic/corruptors/truncation.py. Mirrors the structure of
test_timezone_shift.py -- same contract, different corruption type.
"""

import pandas as pd
import pytest

from wherefore.synthetic.base_dataset import HEALTHCARE_PATIENTS, generate_dataset
from wherefore.synthetic.corruptors.truncation import apply


@pytest.fixture
def healthcare_source():
    return generate_dataset(HEALTHCARE_PATIENTS, n_rows=50, seed=42)


def test_does_not_mutate_input_dataframe(healthcare_source):
    original = healthcare_source.copy(deep=True)
    apply(healthcare_source, column="patient_name", max_length=8, seed=1)
    pd.testing.assert_frame_equal(healthcare_source, original)


def test_affected_rows_are_genuinely_shortened(healthcare_source):
    target, affected = apply(
        healthcare_source, column="patient_name", max_length=8, affected_fraction=0.3, seed=1
    )
    assert len(affected) > 0
    for idx in affected:
        original = str(healthcare_source.loc[idx, "patient_name"])
        truncated = str(target.loc[idx, "patient_name"])
        assert len(truncated) == 8
        assert original.startswith(truncated)
        assert len(original) > 8


def test_rows_already_shorter_than_max_length_are_not_reported_as_affected():
    """
    A row whose value is already <= max_length can't be truncated by
    this operation -- it must not appear in affected_row_indices, since
    that would make the ground truth claim a row was corrupted when it
    wasn't.
    """
    df = pd.DataFrame({"id": [1, 2], "name": ["Al", "Bo"]})  # both very short
    target, affected = apply(df, column="name", max_length=20, affected_fraction=1.0, seed=1)
    assert affected == []
    pd.testing.assert_series_equal(target["name"], df["name"])


def test_unaffected_rows_completely_untouched(healthcare_source):
    target, affected = apply(
        healthcare_source, column="patient_name", max_length=8, affected_fraction=0.3, seed=1
    )
    unaffected = [i for i in range(len(healthcare_source)) if i not in affected]
    assert (
        target.loc[unaffected, "patient_name"] == healthcare_source.loc[unaffected, "patient_name"]
    ).all()


def test_other_columns_untouched(healthcare_source):
    target, _ = apply(healthcare_source, column="patient_name", max_length=8, seed=1)
    for col in healthcare_source.columns:
        if col == "patient_name":
            continue
        pd.testing.assert_series_equal(target[col], healthcare_source[col])


def test_rejects_non_string_column(healthcare_source):
    with pytest.raises(TypeError, match="requires a string column"):
        apply(healthcare_source, column="billed_amount", max_length=5)


def test_rejects_invalid_max_length(healthcare_source):
    with pytest.raises(ValueError, match="max_length"):
        apply(healthcare_source, column="patient_name", max_length=0)


def test_rejects_invalid_affected_fraction(healthcare_source):
    with pytest.raises(ValueError, match="affected_fraction"):
        apply(healthcare_source, column="patient_name", affected_fraction=0.0)


def test_deterministic_given_same_seed(healthcare_source):
    target_a, affected_a = apply(healthcare_source, column="patient_name", max_length=8, seed=99)
    target_b, affected_b = apply(healthcare_source, column="patient_name", max_length=8, seed=99)
    assert affected_a == affected_b
    pd.testing.assert_frame_equal(target_a, target_b)


def test_handles_non_ascii_names_correctly():
    """
    Confirmed by direct testing: non-ASCII names (e.g. 'Renée Brown')
    truncate correctly via Python string slicing -- no encoding
    corruption introduced as a side effect of this corruptor.
    """
    df = pd.DataFrame({"id": [1], "name": ["Renée Brown Garcia"]})
    target, affected = apply(df, column="name", max_length=8, affected_fraction=1.0, seed=1)
    assert affected == [0]
    assert target.loc[0, "name"] == "Renée Br"
