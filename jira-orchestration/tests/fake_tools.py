"""The one fake of the router's activities, shared by every router test.

Records every call and replays a queued answer per tool. By default it behaves
as Jira does: a posted comment gets an id, and editing that comment — how the
"processing" reply becomes the result — succeeds.
"""

from __future__ import annotations

from typing import Any


class Tools:
    """Records every activity call and replays a queued answer per tool."""

    def __init__(self, **answers: Any) -> None:
        # Jira's own behaviour unless a test says otherwise: a posted comment
        # gets an id, and editing it (the "processing" reply) succeeds.
        self.answers = {
            "post_reply": {"posted": True, "comment_id": "20001"},
            "update_reply": {"updated": True},
            **answers,
        }
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args))
        answer = self.answers.get(name)
        if callable(answer):
            return answer(*args)
        if name == "post_reply" and isinstance(answer, dict) and answer.get("posted"):
            # Jira gives every posted comment an id; the reply is edited by it.
            return {"comment_id": "20001", **answer}
        return answer if answer is not None else {}

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def args(self, name: str) -> tuple:
        for called, args in self.calls:
            if called == name:
                return args
        raise AssertionError(f"{name} was never called")

    @property
    def posted_blocks(self) -> list[tuple[str, object]]:
        """What the ONE reply finally says: the last edit of it, or the post."""
        edits = [args for name, args in self.calls if name == "update_reply"]
        posts = [args for name, args in self.calls if name == "post_reply"]
        if edits and self.answers.get("update_reply", {}).get("updated"):
            return list(edits[-1][2])
        return list(posts[-1][1])

    def reply_text(self) -> str:
        out: list[str] = []
        for heading, body in self.posted_blocks:
            out.append(str(heading))
            if isinstance(body, list):
                out.extend(str(x) for x in body)
            else:
                out.append(str(body))
        return "\n".join(out)
