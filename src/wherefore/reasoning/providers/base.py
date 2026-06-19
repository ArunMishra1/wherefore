"""
reasoning/providers/base.py

NEXT TURN: implement this.

Purpose: the ABC every model provider implements. Deliberately minimal
-- one method -- so swapping models is a small, obvious diff.

    class Provider(ABC):
        @abstractmethod
        def complete(self, system_prompt: str, user_prompt: str) -> str:
            '''Send prompt, return raw text response.'''

Keep this interface text-in/text-out. Structured parsing of the
response into ClusterExplanation happens in explain.py, NOT here --
that keeps provider implementations swappable even if we later want a
provider that returns structured JSON natively (e.g. via tool use)
vs. one that returns free text we parse ourselves.
"""
