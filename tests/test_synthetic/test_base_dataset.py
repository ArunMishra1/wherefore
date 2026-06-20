"""
Tests for synthetic/base_dataset.py. Two bugs were caught by hand
during initial implementation -- nanosecond-precision timestamp noise,
and nullable float columns collapsing to object dtype -- both are
covered explicitly below so they can't silently regress.
"""

import pandas as pd
import pytest

from wherefore.synthetic.base_dataset import (
    FINANCIAL_ACCOUNTS,
    HEALTHCARE_PATIENTS,
    generate_dataset,
)


@pytest.mark.parametrize("domain", [FINANCIAL_ACCOUNTS, HEALTHCARE_PATIENTS])
def test_determinism_same_seed_produces_identical_data(domain):
    a = generate_dataset(domain, n_rows=30, seed=42)
    b = generate_dataset(domain, n_rows=30, seed=42)
    assert a.equals(b)


@pytest.mark.parametrize("domain", [FINANCIAL_ACCOUNTS, HEALTHCARE_PATIENTS])
def test_different_seed_produces_different_data(domain):
    a = generate_dataset(domain, n_rows=30, seed=42)
    b = generate_dataset(domain, n_rows=30, seed=43)
    assert not a.equals(b)


@pytest.mark.parametrize("domain", [FINANCIAL_ACCOUNTS, HEALTHCARE_PATIENTS])
def test_all_declared_columns_present(domain):
    df = generate_dataset(domain, n_rows=10, seed=1)
    expected_columns = {f.name for f in domain.fields}
    assert expected_columns == set(df.columns)


@pytest.mark.parametrize("domain", [FINANCIAL_ACCOUNTS, HEALTHCARE_PATIENTS])
def test_key_field_is_present_and_mostly_unique(domain):
    df = generate_dataset(domain, n_rows=100, seed=1)
    # "mostly" unique, not strictly -- the generator deliberately
    # injects a small number of duplicate rows (see dedup_failure
    # fixture design), so exact uniqueness is the wrong assertion here.
    n_unique = df[domain.key_field].nunique()
    assert n_unique >= len(df) * 0.9


def test_financial_datetime_columns_have_second_precision_not_nanosecond_noise():
    """
    Regression test for the nanosecond-precision bug: raw
    rng.integers() over .value produced timestamps like
    '12:06:48.702750148', which doesn't look like real-world data.

    This assertion (dtype == "datetime64[s]") is itself the subject of
    a SECOND regression below -- this one passing depends on
    _gen_datetime explicitly forcing [s] resolution via .astype()
    rather than relying on pd.to_datetime's version-dependent default
    inference. See test_datetime_resolution_is_explicit_not_version_dependent.
    """
    df = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=1)
    assert df["opened_at"].dtype == "datetime64[s]"
    # Every timestamp's sub-second component must be exactly zero.
    assert (df["opened_at"].dt.microsecond == 0).all()
    assert (df["opened_at"].dt.nanosecond == 0).all()


def test_datetime_resolution_is_explicit_not_version_dependent():
    """
    Regression test for a real CI failure: this exact assertion passed
    locally (pandas 3.0.3 was installed, where pd.to_datetime(unit="s")
    happens to infer [s] resolution by default) but failed in a fresh
    CI install, which resolved an earlier pandas 2.x where the same
    call defaults to [ns]. Confirmed via pandas-dev/pandas#55901 and
    related issues that this inference behavior changed across pandas
    versions and is not something to depend on implicitly.

    base_dataset.py's _gen_datetime now forces datetime64[s] explicitly
    via .astype() rather than relying on inference, so this should hold
    regardless of which pandas version (>=2.2, per the tightened floor
    in pyproject.toml) actually gets installed.
    """
    df = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=20, seed=1)
    for col in ("opened_at", "last_transaction_at"):
        assert str(df[col].dtype) == "datetime64[s]", (
            f"{col} resolved to {df[col].dtype}, not datetime64[s] -- "
            "this should be impossible given the explicit .astype() call "
            "in _gen_datetime; if this fails, something upstream of that "
            "call changed."
        )


def test_nullable_float_column_keeps_numeric_dtype_not_object():
    """
    Regression test for the dtype-collapse bug: nullable_fraction > 0
    on a float field previously collapsed the whole column to generic
    object dtype, which breaks downstream numeric operations (e.g.
    float_precision signature detection) and defeats dtype-based
    pattern filtering in taxonomy.registry.patterns_by_dtype.
    """
    df = generate_dataset(HEALTHCARE_PATIENTS, n_rows=200, seed=1)
    assert pd.api.types.is_float_dtype(df["billed_amount"])
    assert df["billed_amount"].isnull().sum() > 0  # nulls actually present
    # Confirm real arithmetic still works post-null-injection.
    assert df["billed_amount"].sum() > 0


def test_non_ascii_names_actually_appear():
    """
    encoding_mismatch corruption needs real non-ASCII content in the
    source to corrupt meaningfully -- confirm the generator actually
    produces some, not just ASCII names with include_non_ascii=True
    silently doing nothing.
    """
    df = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=200, seed=1)
    has_non_ascii = df["customer_name"].apply(lambda s: not s.isascii())
    assert has_non_ascii.sum() > 0


def test_duplicate_rows_are_injected_for_dedup_failure_fixtures():
    df = generate_dataset(FINANCIAL_ACCOUNTS, n_rows=100, seed=1)
    # generate_dataset appends >= 1 duplicate row beyond n_rows.
    assert len(df) > 100
    key_counts = df[FINANCIAL_ACCOUNTS.key_field].value_counts()
    assert (key_counts > 1).any()
