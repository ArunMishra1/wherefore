"""
reasoning/providers/claude.py

Provider implementation against the Anthropic SDK directly (not via a
framework -- direct SDK use means we see exactly what's sent and
returned, which matters for debugging prompt/eval iteration).

Uses forced tool-use to get ClusterExplanation fields back as
structured JSON, rather than asking for JSON in prose and parsing it
out of markdown fences. The tool's input schema is generated directly
from ClusterExplanation.model_json_schema() -- confirmed this produces
a clean, valid schema -- so the tool definition can never silently
drift out of sync with the pydantic model it's supposed to match.

Model choice: defaults to claude-sonnet-4-6 (current generation,
"best combination of speed and intelligence" per Anthropic's own
model-selection guidance -- this task is structured reasoning over a
small amount of data, not a task needing Opus-tier capability).
Configurable via the WHEREFORE_MODEL env var so swapping within
Claude's own lineup doesn't require a code change.
"""

from __future__ import annotations

import json
import os

import anthropic

from wherefore.reasoning.explain import ClusterExplanation
from wherefore.reasoning.providers.base import Provider

DEFAULT_MODEL = "claude-sonnet-4-6"
TOOL_NAME = "submit_cluster_explanation"


class ClaudeProvider(Provider):
    def __init__(self, model: str | None = None, max_tokens: int = 1024):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. The reasoning layer requires "
                "a real Anthropic API key -- set it in your environment "
                "before calling explain()."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model or os.environ.get("WHEREFORE_MODEL", DEFAULT_MODEL)
        self._max_tokens = max_tokens

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """
        Sends the prompt with a forced tool call, so Claude MUST
        respond by invoking submit_cluster_explanation with arguments
        matching ClusterExplanation's schema -- not free text. Returns
        the tool call's input as a JSON string, ready for
        ClusterExplanation.model_validate_json() in explain.py.
        """
        tool_schema = ClusterExplanation.model_json_schema()
        tool = {
            "name": TOOL_NAME,
            "description": "Submit the structured root-cause explanation for this cluster.",
            "input_schema": tool_schema,
        }

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[tool],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == TOOL_NAME:
                return json.dumps(block.input)

        raise RuntimeError(
            f"Claude did not return a {TOOL_NAME} tool call. "
            f"Response content: {response.content}"
        )
