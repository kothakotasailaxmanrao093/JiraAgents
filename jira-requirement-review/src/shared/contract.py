# GENERATED FILE — DO NOT EDIT.
# Mirrored from the canonical shared/ package by scripts/sync_shared.py.
# Edit shared/contract.py at the repository root and re-run that script.
"""The result contract every agent returns to the router.

This is the seam the whole routed architecture rests on. The router composes one
reply from this structure and **never branches on which agent produced it** —
that is what makes a fourth agent a config entry rather than a code change
(Liskov: any child is substitutable behind this type; Open/Closed: the router's
composer grows by table, not by ``if``).

Three rules the fields encode, each of which was a failure mode first:

* ``sources_read`` and ``sources_missing`` are **mandatory**. They may be empty
  lists but never absent. A thin answer with no account of what was read is
  indistinguishable from a badly written ticket, and users blamed the ticket.
* the child **recommends** an email (``email_recommended``); the router decides
  and sends. One decision point means "never email an invalid request" cannot be
  violated by a child that forgets.
* ``degraded`` is reported by the child and *rendered* by the router, which is
  what puts the "please review the wording" warning at the top of the comment.

Plain dataclasses, not pydantic: the review agent keeps its pure layers importable
without a validation library, and this type has to cross an agent boundary as
JSON anyway. :func:`AgentResult.to_dict` / :func:`AgentResult.from_dict` are the
serialisation boundary, and ``CONTRACT_VERSION`` is what lets a router reject a
payload from a child built against an older shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

CONTRACT_VERSION = "1.0"


class Outcome(str, Enum):
    """What the child concluded. The router maps this to a section layout.

    A closed set on purpose: the router's layout table must be exhaustive, and a
    free-form string would let a child invent an outcome nothing knows how to
    render.
    """

    REVIEWED = "REVIEWED"
    CREATED = "CREATED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    NEEDS_INFO = "NEEDS_INFO"
    NOT_A_REQUIREMENT = "NOT_A_REQUIREMENT"
    FAILED = "FAILED"
    # A question about the ticket, answered; nothing created. It used to be
    # sent as REVIEWED, so the answer appeared under "Readiness" with the
    # headline "Nothing was created" (BGV-11, 2026-09-24).
    ANSWERED = "ANSWERED"


class EmailKind(str, Enum):
    CREATED = "created"
    CLARIFICATION = "clarification"
    DUPLICATES = "duplicates"
    FAILED = "failed"
    NONE = "none"


@dataclass
class CreatedIssue:
    """One Jira issue a child actually created."""

    key: str
    issue_type: str = ""
    summary: str = ""
    url: str = ""


@dataclass
class DuplicateRef:
    """Work that already exists, and why the child thinks it matches."""

    proposed_title: str = ""
    existing_key: str = ""
    existing_summary: str = ""
    score: float = 0.0
    matched_on: str = ""
    url: str = ""


@dataclass
class MissingSource:
    """Something the child tried to read and could not.

    ``what_to_do`` is mandatory in spirit: every entry must read as an
    instruction, not an apology. "spec.xlsx could not be opened
    (password-protected) — please re-attach it unprotected", never "some
    attachments were skipped".
    """

    what: str
    why: str = ""
    what_to_do: str = ""

    def as_sentence(self) -> str:
        """The single line a user reads in "What was missing"."""
        parts = self.what
        if self.why:
            parts += f" ({self.why})"
        if self.what_to_do:
            parts += f" — {self.what_to_do}"
        return parts


@dataclass
class AgentResult:
    """What a child hands back. The router turns exactly this into one comment."""

    produced_by: str
    display_name: str
    outcome: Outcome
    headline: str
    summary: str = ""
    agent_version: str = ""
    run_id: str = ""
    contract_version: str = CONTRACT_VERSION

    created: list[CreatedIssue] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    duplicates: list[DuplicateRef] = field(default_factory=list)

    # Mandatory in meaning: present on every run, even when empty.
    sources_read: list[str] = field(default_factory=list)
    sources_missing: list[MissingSource] = field(default_factory=list)

    conflicts: list[str] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""
    email_recommended: bool = False
    email_kind: EmailKind = EmailKind.NONE
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form, for crossing the agent boundary."""
        data = asdict(self)
        data["outcome"] = self.outcome.value
        data["email_kind"] = self.email_kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentResult:
        """Rebuild from a child's payload, tolerating an unknown extra field.

        Unknown keys are ignored rather than raising: a child on a newer build
        that adds a field must not take the router down. A *missing* required
        field is a real error and does raise.
        """
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in (data or {}).items() if k in known}
        payload["outcome"] = Outcome(payload.get("outcome", Outcome.FAILED))
        payload["email_kind"] = EmailKind(payload.get("email_kind", EmailKind.NONE))
        payload["created"] = [_rebuild(CreatedIssue, x) for x in payload.get("created", [])]
        payload["duplicates"] = [_rebuild(DuplicateRef, x) for x in payload.get("duplicates", [])]
        payload["sources_missing"] = [
            _rebuild(MissingSource, x) for x in payload.get("sources_missing", [])
        ]
        return cls(**payload)


def _rebuild(cls: type, value: Any) -> Any:
    """A nested dataclass from a dict, or passed through if already built."""
    if isinstance(value, cls):
        return value
    known = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in (value or {}).items() if k in known})
