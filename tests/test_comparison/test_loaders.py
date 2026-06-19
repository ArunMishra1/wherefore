"""
Tests for comparison/loaders.py. Every scenario here was manually
verified against real pandas behavior before writing the module -- see
loaders.py's module docstring for why encoding failures and null-string
preservation are deliberate, not bugs to fix.
"""

import pandas as pd
import pytest

from wherefore.comparison.loaders import load_csv, load_file, load_json


@pytest.fixture
def csv_with_mixed_nulls(tmp_path):
    p = tmp_path / "test.csv"
    p.write_text("id,name,note\n1,Alice,\n2,Bob,NULL\n3,Carol,NaN\n4,Dave,N/A\n")
    return p


def test_only_truly_empty_cell_becomes_null(csv_with_mixed_nulls):
    """
    Regression-style test for the core loaders.py design decision:
    pandas' default read_csv collapses "NULL", "NaN", "N/A", and an
    empty cell to the same null value -- verified directly before
    writing this module. load_csv must NOT do that.
    """
    df = load_csv(csv_with_mixed_nulls)
    assert df["note"].isnull().sum() == 1  # only Alice's row
    assert df.loc[1, "note"] == "NULL"  # literal string, not null
    assert df.loc[2, "note"] == "NaN"  # literal string, not null
    assert df.loc[3, "note"] == "N/A"  # literal string, not null


def test_strict_utf8_raises_on_latin1_file(tmp_path):
    """
    Confirms load_csv does NOT silently fall back to another encoding
    -- a decode failure must surface as an error, since the failure
    itself is the signal encoding_mismatch needs.
    """
    p = tmp_path / "latin1.csv"
    p.write_bytes("id,name\n1,José\n".encode("latin-1"))

    with pytest.raises(UnicodeDecodeError):
        load_csv(p)


def test_explicit_encoding_override_works(tmp_path):
    p = tmp_path / "latin1.csv"
    p.write_bytes("id,name\n1,José\n".encode("latin-1"))

    df = load_csv(p, encoding="latin-1")
    assert df.loc[0, "name"] == "José"


def test_load_csv_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_csv(tmp_path / "does_not_exist.csv")


def test_load_json_distinguishes_real_null_from_literal_string(tmp_path):
    p = tmp_path / "test.json"
    p.write_text('[{"id": 1, "note": null}, {"id": 2, "note": "NULL"}]')

    df = load_json(p)
    assert df["note"].isnull().sum() == 1
    assert df.loc[1, "note"] == "NULL"


def test_load_file_dispatches_csv(csv_with_mixed_nulls):
    df = load_file(csv_with_mixed_nulls)
    assert len(df) == 4


def test_load_file_dispatches_json(tmp_path):
    p = tmp_path / "test.json"
    p.write_text('[{"id": 1}]')
    df = load_file(p)
    assert len(df) == 1


def test_load_file_raises_on_unrecognized_extension(tmp_path):
    p = tmp_path / "test.txt"
    p.write_text("not a real format")
    with pytest.raises(ValueError, match="Unrecognized file extension"):
        load_file(p)


def test_datetime_like_csv_columns_are_parsed_as_real_datetimes(tmp_path):
    """
    Regression test for a real bug caught while building the CLI:
    without this, a datetime column round-tripped through CSV becomes
    plain dtype 'str', which silently breaks every downstream
    dtype-based pattern match (clustering's patterns_by_dtype finds no
    candidates for a 'str' column, even if the underlying values are
    genuinely timestamps that differ by a constant offset).
    """
    p = tmp_path / "test.csv"
    p.write_text("id,created_at\n1,2024-01-15 10:30:00\n2,2024-01-16 11:45:00\n")

    df = load_csv(p)
    assert pd.api.types.is_datetime64_any_dtype(df["created_at"])


def test_bare_year_column_is_not_falsely_detected_as_datetime(tmp_path):
    """
    Regression guard: confirmed by direct testing that
    pd.to_datetime(..., format='ISO8601') happily parses bare numeric
    strings like "2024" as January 1st of that year -- which would
    silently corrupt a genuine fiscal_year/birth_year column into
    fabricated timestamps. load_csv must not do this.
    """
    p = tmp_path / "test.csv"
    p.write_text("id,fiscal_year\n1,2024\n2,2025\n3,2026\n")

    df = load_csv(p)
    assert not pd.api.types.is_datetime64_any_dtype(df["fiscal_year"])


def test_partially_unparseable_column_is_left_as_string(tmp_path):
    """
    A column that's mostly dates but has one genuinely non-date value
    must NOT be converted -- converting it would require errors='coerce',
    which silently turns the bad value into NaT rather than preserving
    it as evidence of a real data problem.
    """
    p = tmp_path / "test.csv"
    p.write_text("id,maybe_date\n1,2024-01-15 10:30:00\n2,not-a-date\n")

    df = load_csv(p)
    assert df["maybe_date"].dtype.name in ("object", "str")
