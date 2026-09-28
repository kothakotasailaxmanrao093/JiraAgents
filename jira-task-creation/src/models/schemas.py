"""Typed contracts for the work-breakdown agent.

Deterministic rules live here (and in ``validation.py`` / ``jira_service.py``)
rather than in the runtime prompt: the model proposes, these schemas dispose.
Every structure that crosses a tool boundary is validated against these models
before a single Jira write is attempted.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


class ResultStatus(str, Enum):
    """Terminal state of one agent run."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    READY_FOR_JIRA = "READY_FOR_JIRA"
    # Somebody asked a question about the ticket rather than asking for work.
    # The answer is a comment; nothing is created.
    EXPLAINED = "EXPLAINED"
    JIRA_CREATED = "JIRA_CREATED"
    JIRA_CREATION_FAILED = "JIRA_CREATION_FAILED"


class RequestBucket(str, Enum):
    """What kind of comment arrived, and therefore how the agent answers.

    Lives here rather than beside the classifier so the workflow can read it
    without importing the classifier — and with it, everything that reads the
    environment. A Temporal workflow may not touch ``os.environ``.
    """

    NOT_A_REQUIREMENT = "NOT_A_REQUIREMENT"
    INCOMPLETE = "INCOMPLETE"
    VALID = "VALID"
    # Readable, substantial text that names no verb the deterministic list
    # knows. That list is hand-maintained and has been patched three times
    # after live runs dropped real requirements ("Log a rest break", "alert
    # the depot manager"), so an unrecognised verb must not reject on its own.
    # The model decides these; if it is unavailable the caller asks rather
    # than rejects, because a needless question costs a comment and a wrong
    # rejection loses a requirement silently.
    UNCERTAIN = "UNCERTAIN"


class Classification(str, Enum):
    """Scope band of a valid requirement."""

    SMALL = "Small"
    MEDIUM = "Medium"
    LARGE = "Large"


class Priority(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class Complexity(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


NOT_SPECIFIED = "Not specified"
NO_DEPENDENCIES = "None"

# Subtasks describe behaviour, not implementation. A subtask that names a source
# file has drifted into telling engineers which file to edit, which is exactly
# the failure mode this agent must avoid.
_SOURCE_FILE_SUFFIXES = (
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".go",
    ".rb",
    ".cs",
    ".php",
    ".cpp",
    ".c",
    ".h",
    ".rs",
    ".kt",
    ".swift",
    ".sql",
    ".sh",
    ".yaml",
    ".yml",
)


# Filenames the person themselves wrote in the request. Echoing those back is
# not invention, so the guard below lets them through; anything else in a
# sub-task is the agent making up implementation detail, which is refused.
# A ContextVar (not a global) so concurrent runs cannot see each other's list.
_user_supplied_files: ContextVar[frozenset[str]] = ContextVar(
    "_user_supplied_files", default=frozenset()
)


def source_file_tokens(text: str) -> set[str]:
    """Every source-filename-looking token in ``text``, lowercased."""
    found: set[str] = set()
    for token in text.replace("(", " ").replace(")", " ").replace(",", " ").split():
        cleaned = token.strip("`'\"*.:;").lower()
        if cleaned.endswith(_SOURCE_FILE_SUFFIXES) and len(cleaned.split(".")[0]) > 0:
            found.add(cleaned)
    return found


@contextmanager
def allow_source_files_from(requirement: str) -> Iterator[None]:
    """Permit, inside this block, the filenames ``requirement`` already names.

    The no-filenames rule exists to stop the agent inventing source files the
    person never mentioned. When the person writes one themselves it is part of
    the request, and refusing it used to abort the whole run over their own
    words.
    """
    token = _user_supplied_files.set(frozenset(source_file_tokens(requirement or "")))
    try:
        yield
    finally:
        _user_supplied_files.reset(token)


def _invented_source_files(text: str) -> set[str]:
    """Filenames in ``text`` that the person did not supply."""
    return source_file_tokens(text) - _user_supplied_files.get()


class _Strict(BaseModel):
    """Base model: reject unknown fields so the LLM cannot smuggle in extras."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Subtask(_Strict):
    """A unit of work under a Story. Exactly the five contracted fields."""

    title: str = Field(min_length=3, max_length=255)
    description: str = Field(min_length=10)
    expected_outcome: str = Field(min_length=5)
    dependencies: str = Field(default=NO_DEPENDENCIES, min_length=1)
    completion_criteria: str = Field(min_length=5)

    @field_validator("title", "description", "expected_outcome", "completion_criteria")
    @classmethod
    def _no_source_files(cls, value: str) -> str:
        invented = _invented_source_files(value)
        if invented:
            raise ValueError(
                "Subtasks must describe behaviour, not source files the "
                f"requirement never mentioned. Remove {sorted(invented)!r} "
                f"from: {value!r}"
            )
        return value


_SOMEONE_TO = re.compile(r"^([a-z][\w'-]*)(?:\s+[a-z][\w'-]*){0,3}\s+to\s")


def statement_reads_badly(statement: str) -> bool:
    """ "I want send reminders…" — a bare verb straight after "I want".

    A quality check, retried once and then accepted: never a reason to fail a
    build. Accepted forms: "I want to <verb>", "I want a/the <thing>",
    "I want <someone> to <verb>", "I want <things> to be <done>".
    """
    lowered = statement.lower()
    if "i want" not in lowered:
        return False
    after = lowered.split("i want", 1)[1].lstrip()
    if after.startswith(
        ("to ", "a ", "an ", "the ", "my ", "our ", "more ", "all ", "each ", "every ")
    ):
        return False
    if re.match(r"^(?:[a-z][\w'-]*\s+){1,4}to be\b", after):
        return False
    return not _someone_to_act(after)


def _someone_to_act(after_i_want: str) -> bool:
    """ "I want candidates to give consent" — a person, then "to <action>".

    Good English, and rejected until BGV-9 failed a whole build over it
    (2026-09-24). "I want send reminders to referees" also has a "to", so the
    first word must not be a bare action verb.
    """
    from src.classification.validate import _ACTION_WORDS, _verb_stem  # no cycle at import

    match = _SOMEONE_TO.match(after_i_want)
    if not match:
        return False
    first = match.group(1)
    return first not in _ACTION_WORDS and _verb_stem(first) not in _ACTION_WORDS


class Story(_Strict):
    """A user story: the nine contracted fields, plus the questions it leaves open.

    ``open_questions`` were added after the review agent, run on the agent's own
    tickets, found every requirement gap afresh (BGV-76, 2026-09-24). The
    breakdown may not invent a rule the requirement does not state — so it
    lists what a developer will still have to ask, on the ticket, instead.
    """

    title: str = Field(min_length=3, max_length=255)
    user_story_statement: str = Field(min_length=20)
    description: str = Field(min_length=10)
    business_value: str = Field(min_length=5)
    priority: Priority
    estimated_complexity: Complexity
    dependencies: str = Field(default=NO_DEPENDENCIES, min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    subtasks: list[Subtask] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)

    @field_validator("user_story_statement")
    @classmethod
    def _canonical_form(cls, value: str) -> str:
        lowered = value.lower()
        if not (lowered.startswith("as a") or lowered.startswith("as an")):
            raise ValueError(
                "user_story_statement must use the form "
                "'As a [user], I want [action], so that [benefit].'"
            )
        if "i want" not in lowered or "so that" not in lowered:
            raise ValueError("user_story_statement must contain both 'I want' and 'so that'.")
        # The wording after "I want" is NOT checked here. It used to be, and a
        # grammar rule then failed whole builds on good English — "I want
        # candidates to give consent" (BGV-9) and "I want invoices to be sent"
        # (BGV-41, twice, 2026-09-25). Structure is a hard rule; wording is a
        # quality check (`statement_reads_badly`) that asks once, never blocks.
        return value

    @property
    def related_subtasks(self) -> list[str]:
        """Field 9 of the Story contract, derived from the subtasks themselves."""
        return [s.title for s in self.subtasks]


class Epic(_Strict):
    """An epic. Exactly the six contracted business fields — no more.

    ``jira_summary`` is a technical field required by the Jira REST API for the
    issue's Summary; it is deliberately *not* one of the six business fields and
    is derived, never generated as an extra requirement.
    """

    business_objective: str = Field(min_length=10)
    scope: list[str] = Field(min_length=1)
    out_of_scope: list[str] = Field(default_factory=lambda: [NOT_SPECIFIED])
    priority: Priority
    acceptance_criteria: list[str] = Field(min_length=1)
    jira_summary: str = Field(min_length=3, max_length=255)

    @field_validator("out_of_scope")
    @classmethod
    def _default_when_unknown(cls, value: list[str]) -> list[str]:
        cleaned = [item for item in value if item and item.strip()]
        return cleaned or [NOT_SPECIFIED]

    def related_user_stories(self, stories: list[Story]) -> list[str]:
        """Field 6 of the Epic contract, derived from the generated stories."""
        return [story.title for story in stories]


class WorkBreakdown(_Strict):
    """The validated decomposition: classification + optional epic + stories.

    Invariant enforced here (not in the prompt): Small requirements never carry
    an Epic; Medium and Large always do.
    """

    classification: Classification
    analysis: str = Field(min_length=10)
    epic: Epic | None = None
    stories: list[Story] = Field(min_length=1)
    # Avoidable findings the self-review gate could not fix in its one
    # regeneration. Not part of the contract (not dumped); named in the reply.
    _quality_notes: list[str] = PrivateAttr(default_factory=list)
    # The model's line -> ticket mapping, kept so remaining gaps can be named.
    _coverage: dict[str, Any] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _epic_matches_classification(self) -> WorkBreakdown:
        if self.classification is Classification.SMALL:
            if self.epic is not None:
                raise ValueError("A Small requirement must not produce an Epic.")
            if len(self.stories) != 1:
                raise ValueError("A Small requirement must produce exactly one Story.")
        else:
            if self.epic is None:
                raise ValueError(f"A {self.classification.value} requirement must produce an Epic.")
            if len(self.stories) < 2:
                raise ValueError(
                    f"A {self.classification.value} requirement must produce "
                    "at least two Stories."
                )
        return self

    def issue_count(self) -> dict[str, int]:
        return {
            "epics": 1 if self.epic else 0,
            "stories": len(self.stories),
            "subtasks": sum(len(s.subtasks) for s in self.stories),
        }


class ValidationVerdict(_Strict):
    """Outcome of the pre-decomposition gate."""

    status: ResultStatus
    reason: str = ""
    clarifying_questions: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)

    @property
    def may_proceed(self) -> bool:
        return self.status is ResultStatus.READY_FOR_JIRA


class ClarificationRequest(_Strict):
    """A request for the minimum missing information."""

    questions: list[str] = Field(min_length=1, max_length=5)


class JiraIssueRef(_Strict):
    """One issue actually created in Jira. Keys are never fabricated."""

    key: str
    id: str = ""
    url: str = ""
    issue_type: str = ""
    summary: str = ""
    parent_key: str = ""


class SprintPlacement(str, Enum):
    """Where newly created stories should land."""

    BACKLOG = "backlog"
    CURRENT_SPRINT = "current_sprint"


class ProjectInfo(_Strict):
    """A Jira project as the agent actually read it, not as the user described it."""

    key: str
    id: str = ""
    name: str = ""
    description: str = ""
    style: str = ""
    issue_types: list[str] = Field(default_factory=list)

    def as_prompt_text(self) -> str:
        lines = [f"Jira project: {self.name or self.key} ({self.key})"]
        if self.description:
            lines.append(f"Project description (from Jira): {self.description}")
        return "\n".join(lines)


class ExistingIssue(_Strict):
    """A ticket already in the project, used for context and overlap checks.

    ``description`` is carried (truncated) because two tickets can describe the
    same capability with completely different titles — matching on summaries
    alone misses those.
    """

    key: str
    summary: str
    issue_type: str = ""
    status: str = ""
    # Jira's own grouping: "new", "indeterminate" or "done". The status *name*
    # is whatever the team called it ("Won't Do", "Shipped", "Abandoned"), so
    # the category is the only reliable way to tell finished work apart.
    status_category: str = ""
    parent_key: str = ""
    labels: list[str] = Field(default_factory=list)
    description: str = ""
    url: str = ""

    def as_line(self) -> str:
        """One readable line for an email or a comment."""
        bits = [f"{self.key} [{self.issue_type or 'Issue'}]", self.summary]
        if self.status:
            bits.append(f"({self.status})")
        return " ".join(b for b in bits if b)


class EpicContext(_Strict):
    """A validated existing Epic the caller asked to extend."""

    key: str
    summary: str = ""
    description: str = ""
    child_stories: list[ExistingIssue] = Field(default_factory=list)


class SprintInfo(_Strict):
    """The active sprint newly created stories will be added to."""

    id: int
    name: str = ""
    board_id: int = 0
    board_name: str = ""


class DuplicateMatch(_Strict):
    """An existing ticket that substantially overlaps proposed work.

    ``matched_on`` records whether the collision was with the existing ticket's
    summary or with its description — the latter is how work with a differently
    worded title is still caught.
    """

    proposed_title: str
    existing_key: str
    existing_summary: str
    score: float
    matched_on: str = "summary"
    existing_status: str = ""
    existing_type: str = ""
    existing_url: str = ""

    def explain(self) -> str:
        """Why this counts as already-covered, in one sentence."""
        where = "title" if self.matched_on == "summary" else "description"
        return (
            f'"{self.proposed_title}" is already covered by {self.existing_key} '
            f'("{self.existing_summary}", {self.existing_status or "status unknown"}) '
            f"— matched on its {where}, similarity {self.score}."
        )


class OverlapReport(_Strict):
    """Whether existing work covers all, some, or none of a proposal."""

    matches: list[DuplicateMatch] = Field(default_factory=list)
    covered_titles: list[str] = Field(default_factory=list)
    remaining_titles: list[str] = Field(default_factory=list)

    @property
    def is_full(self) -> bool:
        """Everything proposed already exists."""
        return bool(self.covered_titles) and not self.remaining_titles

    @property
    def is_partial(self) -> bool:
        """Some of it exists and some does not."""
        return bool(self.covered_titles) and bool(self.remaining_titles)

    @property
    def blocked(self) -> bool:
        return bool(self.covered_titles)


class JiraContext(_Strict):
    """Everything the agent learned from Jira before deciding anything.

    Gathering this first means an invalid project, a bad epic key, or a missing
    sprint stops the run before a single issue is written.
    """

    ok: bool = True
    error: str = ""
    project: ProjectInfo | None = None
    existing_issues: list[ExistingIssue] = Field(default_factory=list)
    epic: EpicContext | None = None
    sprint: SprintInfo | None = None
    placement: SprintPlacement = SprintPlacement.BACKLOG
    notes: list[str] = Field(default_factory=list)
    # True when the failure is something the requester can answer (for example
    # "which sprint?"), rather than a broken configuration. Decides whether the
    # run reports a failure or asks a question.
    needs_clarification: bool = False
    # Anything that could not be read — an attachment, a page of results, an
    # activity feed. Reported rather than silently missing.
    unavailable: list[str] = Field(default_factory=list)


class JiraResult(_Strict):
    """Outcome of the Jira write phase, including partial failures."""

    status: ResultStatus
    epic: JiraIssueRef | None = None
    stories: list[JiraIssueRef] = Field(default_factory=list)
    subtasks: list[JiraIssueRef] = Field(default_factory=list)
    created_keys: list[str] = Field(default_factory=list)
    # Keys that already existed and were reused. Disjoint from created_keys:
    # a run either created an issue or found it, never both.
    reused_keys: list[str] = Field(default_factory=list)
    failed_issue: str = ""
    failure_reason: str = ""
    retry_safe: bool = True
    idempotency_key: str = ""
    reused_existing: bool = False
    summary: str = ""
    epic_reused: bool = False
    sprint: SprintInfo | None = None
    sprint_error: str = ""
    placement: SprintPlacement = SprintPlacement.BACKLOG
    # Set when sub-tasks were attached to an existing ticket rather than a new
    # Story being created for them.
    attached_to: str = ""


class TicketWiseResult(_Strict):
    """The agent's structured return value. Only relevant fields are populated."""

    status: ResultStatus
    message: str = ""
    classification: Classification | None = None
    analysis: str = ""
    epic: dict[str, Any] | None = None
    stories: list[dict[str, Any]] = Field(default_factory=list)
    jira_result: dict[str, Any] | None = None
    clarifying_questions: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    generator: str = ""
    # Avoidable findings the self-review gate could not fix (D1); named in the reply.
    quality_notes: list[str] = Field(default_factory=list)
    jira_context: dict[str, Any] | None = None
    duplicate_matches: list[dict[str, Any]] = Field(default_factory=list)
    # The closest existing ticket even when it did not cross the threshold, so
    # a near miss is visible instead of looking like "nothing similar exists".
    best_match: dict[str, Any] | None = None
    # A flat, one-level view of the headline facts, for reading at a glance
    # rather than digging through the nested structure above.
    overview: dict[str, Any] = Field(default_factory=dict)
    # The same thing rendered as plain text, ready to paste into a chat or ticket.
    summary_text: str = ""
    # Populated only in webhook mode: the issue that triggered the run.
    source_issue: dict[str, Any] | None = None
    # Populated whenever an email was attempted, successful or not.
    notification: dict[str, Any] | None = None
    # Set when the run stopped before doing any work (wrong keyword, already done).
    skipped_reason: str = ""


# --------------------------------------------------------------------------
# Webhook ingestion: everything read from the Jira issue that triggered the run
# --------------------------------------------------------------------------


class AttachmentText(_Strict):
    """One attachment and whatever text could be extracted from it.

    ``text`` is empty when extraction was skipped or failed; ``note`` then says
    why. A failed attachment never fails the run — a requirement is still
    readable without its screenshot.
    """

    filename: str
    mime_type: str = ""
    size_bytes: int = 0
    text: str = ""
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


class ConfluencePage(_Strict):
    """One Confluence page a ticket links to, and whatever text it held.

    ``text`` is empty when the page could not be read; ``note`` then says why.
    An unreadable page never fails the run, but it is always reported — a
    linked spec that silently contributed nothing is how a thin breakdown gets
    mistaken for a bad requirement.
    """

    page_id: str = ""
    title: str = ""
    space_key: str = ""
    url: str = ""
    text: str = ""
    note: str = ""
    # Files attached to the page. A spec often lives in the spreadsheet hanging
    # off the page rather than in the page body.
    attachments: list[AttachmentText] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


class CommentInfo(_Strict):
    """One comment on the triggering issue, flattened out of ADF."""

    id: str = ""
    author: str = ""
    created: str = ""
    body: str = ""


class HistoryEntry(_Strict):
    """One field change from the issue changelog."""

    created: str = ""
    author: str = ""
    field: str = ""
    from_value: str = ""
    to_value: str = ""

    def as_line(self) -> str:
        return (
            f"{self.created} {self.author} changed {self.field} "
            f"from '{self.from_value or '-'}' to '{self.to_value or '-'}'"
        )


class WorklogEntry(_Strict):
    """One work-log entry."""

    author: str = ""
    started: str = ""
    time_spent: str = ""
    comment: str = ""


class LinkedIssue(_Strict):
    """An issue linked to the trigger issue, or its parent/child."""

    key: str
    summary: str = ""
    issue_type: str = ""
    status: str = ""
    relationship: str = ""


class SourceIssue(_Strict):
    """The Jira issue that triggered the run, read in full.

    ``requirement_text`` is the assembled input handed to the pipeline: the
    description first, then attachment and linked-page text, comments, links
    and activity, each
    under a labelled heading so the model can tell a stated requirement from
    surrounding discussion.
    """

    ok: bool = True
    error: str = ""
    key: str = ""
    project_key: str = ""
    project_name: str = ""
    summary: str = ""
    description: str = ""
    issue_type: str = ""
    status: str = ""
    reporter: str = ""
    created: str = ""
    labels: list[str] = Field(default_factory=list)
    attachments: list[AttachmentText] = Field(default_factory=list)
    confluence_pages: list[ConfluencePage] = Field(default_factory=list)
    comments: list[CommentInfo] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
    worklogs: list[WorklogEntry] = Field(default_factory=list)
    linked_issues: list[LinkedIssue] = Field(default_factory=list)
    requirement_text: str = ""
    # Where the ticket and a linked page state different numbers for one thing.
    # Reported, never resolved: the agent does not get to decide which is right.
    conflicts: list[str] = Field(default_factory=list)
    trigger_matched: bool = False
    already_processed: bool = False
    notes: list[str] = Field(default_factory=list)
    # Where the requester asked for the stories to go, read out of their own
    # words ("Add the stories to the current sprint"). Defaults to the backlog.
    requested_placement: SprintPlacement = SprintPlacement.BACKLOG
    # The comment that asked for this. A request is made by commenting on a
    # ticket, so the comment is the requirement and the ticket is context.
    trigger_comment_id: str = ""
    trigger_comment_author: str = ""
    trigger_comment_body: str = ""
    # True when the comment asked the agent to work from this ticket rather
    # than stating a requirement of its own.
    trigger_is_pointer: bool = False
    # These three are decided while the issue is read — inside an activity —
    # because working them out needs the trigger keyword, and that comes from
    # the environment. A Temporal workflow may not read os.environ at all, so
    # the agent receives the answers rather than computing them.
    trigger_is_question: bool = False
    request_is_only_link: bool = False
    unread_links: list[str] = Field(default_factory=list)
    # The prose answer to a question, prepared here for the same reason.
    explanation: str = ""

    def source_counts(self) -> dict[str, int]:
        """What the assembled requirement was actually built from."""
        return {
            "attachments": len([a for a in self.attachments if a.usable]),
            "confluence_pages": len([p for p in self.confluence_pages if p.usable]),
            "comments": len(self.comments),
            "history": len(self.history),
            "worklogs": len(self.worklogs),
            "linked_issues": len(self.linked_issues),
        }


class NotificationKind(str, Enum):
    """Why an email is being sent. Each maps to one subject/body template."""

    CLARIFICATION_REQUIRED = "clarification_required"
    DUPLICATES_FOUND = "duplicates_found"
    PARTIAL_DUPLICATE = "partial_duplicate"
    INVALID_REQUEST = "invalid_request"
    FAILED = "failed"
    CREATED = "created"


class NotificationResult(_Strict):
    """Outcome of one email attempt. A send failure never fails the agent run."""

    attempted: bool = False
    sent: bool = False
    kind: NotificationKind | None = None
    recipients: list[str] = Field(default_factory=list)
    subject: str = ""
    error: str = ""
