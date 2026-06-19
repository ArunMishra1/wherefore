"""
taxonomy/registry.py

Loads every YAML file in taxonomy/patterns/, validates each against
PatternDefinition, and exposes a single lookup API for the rest of the
codebase. This is the file that makes "adding a pattern is additive, not
a core-code change" literally true: drop a new valid YAML file in
patterns/, and it shows up everywhere (clustering, LLM prompt menu,
synthetic generator) with zero other edits.

If a pattern file is malformed, loading fails LOUDLY at import time with
a message pointing at the specific file and field -- this is deliberate.
A silently-skipped broken pattern would be far worse than a startup crash.
"""

from __future__ import annotations

import importlib
from functools import lru_cache
from pathlib import Path
from typing import Callable

import yaml
from pydantic import ValidationError

from wherefore.taxonomy.schema import PatternDefinition

PATTERNS_DIR = Path(__file__).parent / "patterns"


class TaxonomyLoadError(Exception):
    """Raised when a pattern YAML file fails validation. Wraps the
    original pydantic error with the offending file path for a useful
    error message."""


def _load_pattern_file(path: Path) -> PatternDefinition:
    with open(path) as f:
        raw = yaml.safe_load(f)

    try:
        pattern = PatternDefinition.model_validate(raw)
    except ValidationError as e:
        raise TaxonomyLoadError(
            f"Pattern file {path.name} failed validation:\n{e}"
        ) from e

    if pattern.id != path.stem:
        raise TaxonomyLoadError(
            f"Pattern file {path.name} has id={pattern.id!r}, "
            f"which must match the filename stem ({path.stem!r})."
        )

    return pattern


@lru_cache(maxsize=1)
def load_all_patterns() -> dict[str, PatternDefinition]:
    """
    Load and validate every pattern in taxonomy/patterns/.
    Cached -- patterns are static within a process lifetime. Tests that
    need to reload after adding a fixture pattern should call
    `load_all_patterns.cache_clear()` first.
    """
    if not PATTERNS_DIR.exists():
        raise TaxonomyLoadError(f"Patterns directory not found: {PATTERNS_DIR}")

    patterns: dict[str, PatternDefinition] = {}
    for yaml_path in sorted(PATTERNS_DIR.glob("*.yaml")):
        pattern = _load_pattern_file(yaml_path)
        if pattern.id in patterns:
            raise TaxonomyLoadError(f"Duplicate pattern id: {pattern.id!r}")
        patterns[pattern.id] = pattern

    if not patterns:
        raise TaxonomyLoadError(
            f"No valid pattern files found in {PATTERNS_DIR}. "
            "At least one pattern is required."
        )

    return patterns


def get_pattern(pattern_id: str) -> PatternDefinition:
    patterns = load_all_patterns()
    if pattern_id not in patterns:
        raise KeyError(
            f"Unknown pattern id: {pattern_id!r}. "
            f"Known patterns: {sorted(patterns.keys())}"
        )
    return patterns[pattern_id]


def patterns_by_dtype(dtype: str) -> list[PatternDefinition]:
    """Used by clustering to narrow which patterns are even candidates
    for a given column's dtype, before running signature checks."""
    return [
        p
        for p in load_all_patterns().values()
        if any(dtype in hint.applies_to_dtypes for hint in p.detection_hints)
    ]


def resolve_import_path(import_path: str) -> Callable:
    """
    Resolves 'module.path:function_name' strings from
    synthetic_corruption.generator or confirmation_function into actual
    callables. Used by both the synthetic generator and clustering code,
    so it lives here rather than being duplicated in both places.
    """
    module_path, func_name = import_path.split(":", 1)
    module = importlib.import_module(module_path)
    try:
        return getattr(module, func_name)
    except AttributeError as e:
        raise TaxonomyLoadError(
            f"Could not resolve {import_path!r}: "
            f"module {module_path!r} has no attribute {func_name!r}"
        ) from e


def build_llm_taxonomy_menu() -> str:
    """
    Builds the plain-English taxonomy menu injected into the reasoning
    layer's system prompt -- the list of "things I know how to recognize"
    that the LLM is told about, so it can match against them OR
    explicitly say a cluster doesn't fit any known pattern.

    Deliberately includes only `display_name` and `description`, NOT
    `llm_context` -- llm_context is pattern-specific detail injected
    only when that pattern's hint already fired, to keep this menu
    compact regardless of how many patterns exist.
    """
    lines = []
    for pattern in load_all_patterns().values():
        lines.append(f"- {pattern.display_name} ({pattern.id}): {pattern.description}")
    return "\n".join(lines)
