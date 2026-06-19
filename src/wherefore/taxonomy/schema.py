"""
taxonomy/schema.py

This module defines the SHAPE of a failure pattern. Every YAML file in
taxonomy/patterns/ is validated against `PatternDefinition` at load time.

Why this exists as a strict schema rather than "just read the YAML as a dict":
  - A malformed pattern file should fail loudly at startup, not silently
    produce a pattern that never matches anything or crashes mid-report.
  - Contributors adding pattern #9 should get clear validation errors
    pointing at exactly what's missing/wrong, without reading clustering
    or reasoning code first.

Design note on `detection_hints`:
  v1 deliberately supports ONE statistical signature per pattern (see
  README / CONTRIBUTING for the rationale). Patterns that genuinely need
  compound signals (e.g. dedup_failure needing both a row-count delta AND
  a duplicate-key signature) declare their primary signature here for
  cheap candidate filtering, and implement a `confirm()` function in
  their corruptor module for the secondary check. This is an intentional
  escape hatch, not an inconsistency -- see registry.py for how it's wired.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Category(str, Enum):
    TEMPORAL = "temporal"
    ENCODING = "encoding"
    TYPE_COERCION = "type_coercion"
    STRUCTURAL = "structural"  # dedup, key mismatch
    NUMERIC = "numeric"
    REFERENCE_DATA = "reference_data"  # enum/lookup drift


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DetectionHint(BaseModel):
    """
    A single cheap, deterministic statistical signature that the
    clustering step checks BEFORE any LLM call. This is what allows the
    system to honestly say "unrecognized pattern" -- if no pattern's
    hint fires for a cluster, the LLM is told explicitly that nothing
    in the known taxonomy matched.

    `signature` is a string key that clustering/cluster_mismatches.py
    dispatches on to find the actual detector function. Keeping this as
    a string (rather than embedding logic in YAML) is the v1 simplicity
    tradeoff -- see module docstring.
    """

    applies_to_dtypes: list[str] = Field(
        description="dtypes this hint is relevant for, e.g. ['datetime', 'timestamp']"
    )
    signature: str = Field(
        description="Dispatch key matched against registered detector functions "
        "in clustering/signatures.py, e.g. 'constant_offset_subset'"
    )
    description: str = Field(
        description="Human-readable explanation of what this signature detects, "
        "shown in --verbose output and used in registry validation errors"
    )


class SyntheticCorruptionSpec(BaseModel):
    """
    Points at the corruptor function that generates labeled fixtures for
    this pattern, plus the schema for its tunable parameters. This is
    the link between a taxonomy pattern and its eval ground truth --
    one pattern definition drives both detection AND test data generation.
    """

    generator: str = Field(
        description="Import path to corruptor function, format "
        "'module.path:function_name', e.g. "
        "'wherefore.synthetic.corruptors.timezone_shift:apply'"
    )
    params_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="Tunable parameters for the corruptor, e.g. "
        "{'offset_hours': {'type': 'float', 'default': 5.0}}. "
        "Kept as a loose dict in v1 rather than a nested model -- "
        "params vary too much per-pattern to standardize yet.",
    )

    @field_validator("generator")
    @classmethod
    def must_have_colon_separator(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError(
                f"generator must be 'module.path:function_name', got: {v!r}"
            )
        return v


class PatternDefinition(BaseModel):
    """
    The full contract for a taxonomy pattern YAML file. See
    taxonomy/patterns/timezone_shift.yaml for a worked example of every
    field in context.
    """

    id: str = Field(description="Unique snake_case identifier, must match filename")
    display_name: str
    category: Category
    description: str = Field(
        description="What this pattern IS, in plain English. This text is "
        "shown to the LLM as part of the taxonomy menu, so write it for "
        "that audience, not just for humans reading the YAML."
    )

    detection_hints: list[DetectionHint] = Field(
        min_length=1,
        description="v1: exactly one hint per pattern is the convention, "
        "though the schema technically allows more for forward-compat. "
        "See module docstring on the single-signature decision.",
    )

    llm_context: str = Field(
        description="Pattern-specific domain knowledge injected into the "
        "prompt ONLY when this pattern's hint fires for a cluster. Keep "
        "this focused -- it costs prompt tokens on every matched cluster."
    )

    synthetic_corruption: SyntheticCorruptionSpec

    severity_default: Severity = Severity.MEDIUM
    references: list[str] = Field(default_factory=list)

    # Optional escape hatch for compound-signature patterns (see module
    # docstring). If set, clustering code will call this after the
    # detection_hint fires, to confirm before accepting the match.
    confirmation_function: str | None = Field(
        default=None,
        description="Optional import path 'module:function' for a secondary "
        "confirmation check, used by patterns needing compound signals "
        "(e.g. dedup_failure). None means the detection_hint alone is "
        "sufficient to accept the match.",
    )

    @field_validator("id")
    @classmethod
    def id_is_snake_case(cls, v: str) -> str:
        if not v.replace("_", "").isalnum() or not v.islower():
            raise ValueError(f"id must be snake_case, got: {v!r}")
        return v
