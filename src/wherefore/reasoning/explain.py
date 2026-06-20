"""
reasoning/explain.py

The single `explain()` interface the rest of the codebase calls,
regardless of which underlying model/provider answers it.

Flow:
    explanation = explain(cluster, taxonomy_menu)
        -> provider = get_provider()          # reads env, returns a Provider
        -> prompt = build_prompt(cluster, taxonomy_menu)
        -> raw_json = provider.complete(system_prompt, user_prompt)
        -> return ClusterExplanation.model_validate_json(raw_json)

`ClusterExplanation` IS the eval target later -- evals/harness/scoring.py
will compare `matched_pattern_id` against synthetic ground truth to
compute precision/recall per corruption type. Keep this schema stable
once evals are running against it.

Design note on Provider.complete()'s signature: it stays plain
text-in/text-out (system_prompt, user_prompt) -> str, even though the
Claude provider internally uses tool-use to FORCE that string to be
valid JSON. This keeps providers swappable -- a future provider that
doesn't support native tool-use can still implement the same
interface by returning a JSON string however it manages to produce
one (e.g. asking for JSON in prose and accepting the parsing risk).
Reliability of the Claude implementation specifically comes from HOW
ClaudeProvider implements complete(), not from the interface itself.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from wherefore.clustering.cluster_mismatches import Cluster
from wherefore.reasoning.providers.base import Provider

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "cluster_explanation_v1.md"
MAX_EXAMPLE_ROWS_IN_PROMPT = 8  # cap prompt size; report.py can show more from the cluster directly


class CitedRow(BaseModel):
    """One example row cited in the explanation. Keeping this as
    structured data (not free text embedded in the narrative) is what
    lets report.py render it consistently and lets the eval harness
    later check that cited rows are real rows from the cluster, not
    fabricated."""

    key: dict
    source_value: str
    target_value: str


class ClusterExplanation(BaseModel):
    """
    The structured output of the reasoning layer for one cluster. This
    is what explain() returns, what the Claude provider is forced (via
    tool-use) to produce, and what the eval harness will eventually
    score against ground truth.
    """

    matched_pattern_id: str | None = Field(
        description="The pattern_id this cluster's cause was attributed to, "
        "or null if the LLM concluded none of the candidate patterns "
        "(or no pattern at all) actually explains it."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="The LLM's own confidence in this attribution -- "
        "independent of, and not required to match, clustering's "
        "statistical confidence score for the same pattern.",
    )
    narrative: str = Field(
        description="2-4 sentence plain-English explanation of the likely "
        "cause, written for a data engineer who has 30 seconds."
    )
    cited_rows: list[CitedRow] = Field(
        default_factory=list,
        description="Specific example rows supporting the narrative.",
    )

    @field_validator("narrative")
    @classmethod
    def narrative_is_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("narrative must not be empty")
        return v


def build_prompt(cluster: Cluster, taxonomy_menu: str) -> tuple[str, str]:
    """
    Builds (system_prompt, user_prompt) from the versioned template.
    Returns the literal text Claude (or any provider) receives -- kept
    as a separate, testable function so prompt construction can be
    unit-tested without any API call.
    """
    template = PROMPT_TEMPLATE_PATH.read_text()

    # Strip the leading HTML comment block (dev-facing notes about
    # prompt versioning -- not meant for the model). Confirmed by
    # direct testing that without this, the entire comment leaked into
    # the system prompt verbatim, since "# System Prompt" appears
    # AFTER the comment closes, not before it.
    if template.startswith("<!--"):
        _, _, template = template.partition("-->")

    system_part, _, user_template = template.partition("# User Prompt Template")

    system_prompt = system_part.replace("# System Prompt", "").strip()
    system_prompt = system_prompt.replace("{taxonomy_menu}", taxonomy_menu)

    example_rows = cluster.mismatches[:MAX_EXAMPLE_ROWS_IN_PROMPT]
    example_rows_text = "\n".join(
        f"- key={m.key!r}: source={m.source_value!r} -> target={m.target_value!r}"
        for m in example_rows
    )
    if len(cluster.mismatches) > MAX_EXAMPLE_ROWS_IN_PROMPT:
        remaining = len(cluster.mismatches) - MAX_EXAMPLE_ROWS_IN_PROMPT
        row_word = "row" if remaining == 1 else "rows"
        example_rows_text += f"\n- ... and {remaining} more {row_word} not shown"

    if cluster.candidate_patterns:
        candidates_text = "\n".join(
            f"- {p.pattern_id} (signature '{p.signature_name}' fired with "
            f"statistical confidence {p.confidence:.2f})"
            for p in cluster.candidate_patterns
        )
    else:
        candidates_text = "None -- no known pattern's statistical signature matched this cluster."

    user_prompt = (
        user_template.strip()
        .replace("{cluster_summary}", f"Column `{cluster.column}`, {len(cluster.mismatches)} mismatched row(s).")
        .replace("{detection_hint_result}", candidates_text)
        .replace("{candidate_patterns}", candidates_text)
        .replace("{example_rows}", example_rows_text)
    )

    return system_prompt, user_prompt


def explain(
    cluster: Cluster,
    taxonomy_menu: str,
    provider: Provider | None = None,
    redact: bool = True,
) -> tuple[ClusterExplanation, list[str]]:
    """
    Takes a statistically-analyzed cluster and produces a plain-English
    explanation. `provider` defaults to the Claude provider if not
    given (lazy import to avoid requiring the anthropic SDK / API key
    for callers who only need build_prompt or testing with a fake
    provider).

    `redact` defaults to True: before building the prompt, the
    cluster's mismatch values are passed through
    reasoning.redaction.redact_mismatch_rows, which masks common
    structured sensitive patterns (emails, SSNs, credit card numbers,
    phone numbers) -- see redaction.py's module docstring for the
    honest scope of what this does and doesn't catch. This is
    deliberately secure-by-default: redaction happens unless a caller
    explicitly passes redact=False, not the other way around, since
    opt-in redaction tends to mean most people never enable it.

    Returns (explanation, redaction_categories_found) -- NOT bundled
    into ClusterExplanation itself, since that schema is also what
    Claude is FORCED to populate via tool-use (see providers/claude.py);
    redaction metadata describes the INPUT, not something the model
    should be asked to report about itself. redaction_categories_found
    is [] when redact=False or when nothing matched any pattern --
    callers (e.g. cli.py) use this to tell the user what was masked.
    """
    if redact:
        from wherefore.reasoning.redaction import redact_mismatch_rows

        redacted_mismatches, categories_found = redact_mismatch_rows(cluster.mismatches)
        # Build a redacted COPY of the cluster for prompt construction
        # only -- never mutate the original, since callers (e.g. the
        # CLI's report renderer) still need the real, unredacted
        # mismatches for the statistical-evidence section of the report.
        from dataclasses import replace

        cluster_for_prompt = replace(cluster, mismatches=redacted_mismatches)
    else:
        cluster_for_prompt = cluster
        categories_found = []

    if provider is None:
        from wherefore.reasoning.providers.claude import ClaudeProvider

        provider = ClaudeProvider()

    system_prompt, user_prompt = build_prompt(cluster_for_prompt, taxonomy_menu)
    raw_json = provider.complete(system_prompt, user_prompt)

    try:
        explanation = ClusterExplanation.model_validate_json(raw_json)
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(
            f"Provider returned a response that doesn't match ClusterExplanation: {e}\n"
            f"Raw response: {raw_json[:500]}"
        ) from e

    return explanation, categories_found
