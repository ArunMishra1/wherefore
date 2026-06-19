"""
Real tests against the taxonomy registry -- the one module that's
fully implemented so far. Future modules should follow this pattern:
test the public API (registry functions), not implementation details,
so internals can change without breaking tests.
"""

import pytest
import yaml

from wherefore.taxonomy import registry
from wherefore.taxonomy.registry import TaxonomyLoadError
from wherefore.taxonomy.schema import PatternDefinition


def test_load_all_patterns_includes_timezone_shift():
    patterns = registry.load_all_patterns()
    assert "timezone_shift" in patterns
    assert isinstance(patterns["timezone_shift"], PatternDefinition)


def test_get_pattern_unknown_id_raises_with_known_list():
    with pytest.raises(KeyError, match="Unknown pattern id"):
        registry.get_pattern("not_a_real_pattern")


def test_build_llm_taxonomy_menu_contains_display_names():
    menu = registry.build_llm_taxonomy_menu()
    assert "Timezone Conversion Inconsistency" in menu
    # llm_context should NOT leak into the compact menu (see registry
    # docstring: menu is deliberately compact regardless of pattern count)
    assert "EST/EDT" not in menu


def test_patterns_by_dtype_filters_correctly():
    datetime_patterns = registry.patterns_by_dtype("datetime")
    assert any(p.id == "timezone_shift" for p in datetime_patterns)

    unrelated = registry.patterns_by_dtype("totally_made_up_dtype")
    assert unrelated == []


def test_patterns_by_dtype_matches_real_pandas_dtype_strings():
    """
    Regression test for a real bug: patterns_by_dtype originally did
    exact string matching, so a YAML declaring applies_to_dtypes:
    ["datetime"] never matched real pandas dtype strings like
    'datetime64[s]', 'datetime64[ns]', or 'datetime64[us]' -- meaning
    NO cluster ever matched timezone_shift against real diff output,
    despite the underlying signature scoring correctly in isolation.
    Fixed via dtype-family matching in _dtype_matches_family.
    """
    for real_dtype_string in ["datetime64[s]", "datetime64[ns]", "datetime64[us]"]:
        matched = registry.patterns_by_dtype(real_dtype_string)
        assert any(p.id == "timezone_shift" for p in matched), (
            f"timezone_shift should match real dtype string {real_dtype_string!r}"
        )

    # Sanity: unrelated real dtype strings should NOT match
    for unrelated_dtype in ["float64", "Float64", "str", "int64"]:
        matched = registry.patterns_by_dtype(unrelated_dtype)
        assert not any(p.id == "timezone_shift" for p in matched), (
            f"timezone_shift should NOT match unrelated dtype string {unrelated_dtype!r}"
        )


def test_resolve_import_path_for_timezone_corruptor():
    """
    Now that synthetic/corruptors/timezone_shift.py implements apply(),
    this test exercises the real resolution path end-to-end rather than
    just checking import-path syntax (see git history for the prior
    placeholder version of this test).
    """
    generator_path = registry.get_pattern("timezone_shift").synthetic_corruption.generator
    apply_fn = registry.resolve_import_path(generator_path)

    import pandas as pd

    df = pd.DataFrame({"ts": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"])})
    corrupted, affected = apply_fn(df, column="ts", offset_hours=5.0, affected_fraction=1.0, seed=1)
    assert len(affected) == 3
    assert (corrupted["ts"] - df["ts"] == pd.Timedelta(hours=5)).all()


def test_malformed_pattern_file_raises_taxonomy_load_error(tmp_path, monkeypatch):
    """Confirms the loud-failure guarantee: a broken pattern YAML
    should never be silently skipped."""
    bad_dir = tmp_path / "patterns"
    bad_dir.mkdir()
    (bad_dir / "broken_pattern.yaml").write_text(
        yaml.dump({"id": "broken_pattern", "display_name": "Incomplete"})
    )

    monkeypatch.setattr(registry, "PATTERNS_DIR", bad_dir)
    registry.load_all_patterns.cache_clear()

    with pytest.raises(TaxonomyLoadError, match="failed validation"):
        registry.load_all_patterns()

    # Restore cache state for other tests in the suite
    registry.load_all_patterns.cache_clear()
