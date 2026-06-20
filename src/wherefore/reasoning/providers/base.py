"""
reasoning/providers/base.py

The ABC every model provider implements. Deliberately minimal -- one
method -- so swapping models is a small, obvious diff.

Interface is plain text-in/text-out, even though ClaudeProvider
internally uses tool-use to FORCE its output to be valid JSON (see
explain.py's module docstring for the reasoning). The interface itself
doesn't assume any particular mechanism for achieving that reliability
-- a future provider can implement it however it needs to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """
        Send the prompt, return a raw string response. The caller
        (explain.py) expects this string to be parseable as a
        ClusterExplanation JSON object -- it's the provider's job to
        make that reliably true, by whatever mechanism it has
        available (tool-use, structured output mode, careful prompting
        plus retry, etc.).
        """
        raise NotImplementedError
