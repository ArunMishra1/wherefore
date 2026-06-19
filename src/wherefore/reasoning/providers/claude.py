"""
reasoning/providers/claude.py

NEXT TURN: implement this.

Purpose: Provider implementation against the Anthropic SDK directly
(not via a framework -- see pyproject.toml notes on why). Reads API key
from environment (ANTHROPIC_API_KEY), instantiates the SDK client,
implements `complete()`.

Model choice: default to a current Claude model; make this configurable
(env var or CLI flag) rather than hardcoded, since "swappable later"
should apply within Claude's own model lineup too, not just across
vendors.

Worth deciding when implementing: should this use plain text completion
or Claude's tool-use / structured output capability to get
ClusterExplanation fields directly as structured JSON, skipping a
separate parsing step? Structured tool-use output is likely more
reliable than asking for JSON in prose and parsing it, and avoids a
whole class of "LLM wrapped JSON in markdown fences" parsing bugs.
"""
