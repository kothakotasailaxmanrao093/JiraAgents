"""Core context abstractions: ``ContextDocument``, ``FetchContext``, ``ContextSource``.

These are framework-free. Sources perform async I/O in ``load`` and therefore must
only ever be run inside the ``gather_context`` activity (never in the deterministic
workflow). Constructing a source must NOT do I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextDocument:
    """One labeled piece of context the LLM may cite as evidence.

    ``source_label`` is the human-readable string the LLM must echo verbatim in a
    finding's ``evidence_source`` (e.g. ``"Target ABC-123 (description)"``).
    """

    source_id: str
    source_label: str
    kind: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_label": self.source_label,
            "kind": self.kind,
            "text": self.text,
            "metadata": self.metadata,
        }


@dataclass
class FetchContext:
    """Inputs + shared state threaded through the context sources during a gather."""

    issue_key: str
    include_parent: bool = True
    include_subtasks: bool = True
    include_linked_issues: bool = True
    include_attachments: bool = True
    include_confluence: bool = True
    # Set only on the webhook path: the comment that asked for the review.
    trigger_comment_id: str | None = None
    # Storage keys of the files uploaded on the form (transcripts, specs …).
    transcript_file_keys: list[str] = field(default_factory=list)
    team_id: str | None = None
    # populated during gather:
    target_bundle: dict[str, Any] | None = None
    jira: Any = None  # JiraClient — typed Any to keep this module SDK/aiohttp-free
    warnings: list[str] = field(default_factory=list)


class ContextSource(ABC):
    source_id: str = "source"

    def is_enabled(self, ctx: FetchContext) -> bool:
        """Whether this source should run for the given context. Default: yes."""
        return True

    @abstractmethod
    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        """Return this source's documents. Should degrade (return ``[]`` + a
        ``ctx.warnings`` note) rather than raise on recoverable problems."""
        raise NotImplementedError
