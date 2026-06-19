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


def test_resolve_import_path_for_timezone_corruptor_target():
    # We're not calling the function (it's not implemented yet), just
    # confirming the import path syntax resolves to a real module --
    # this will start failing once corruptors/timezone_shift.py is
    # filled in, which is the point: it should resolve successfully
    # once the module exists, and the module currently exists as a
    # stub, so importing it should succeed even though `apply` doesn't
    # exist yet.
    module_path, func_name = (
        registry.get_pattern("timezone_shift").synthetic_corruption.generator.split(":")
    )
    import importlib

    module = importlib.import_module(module_path)
    assert not hasattr(module, func_name), (
        "This test should start failing (in a good way) once "
        "synthetic/corruptors/timezone_shift.py implements apply() -- "
        "update this test to actually call resolve_import_path() then."
    )


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
