"""The self-review gate: score a draft breakdown against the shared rubric
before anything is written to Jira (D1, 2026-09-25).

The prompt already demanded testable criteria and forbade inventing rules;
nothing checked the answer, and the review agent then found the same avoidable
gaps on the agent's own tickets (BGV-76, BGV-94, BGV-108). This checks.

Two kinds of finding, from ``src/shared/rubric.py``:

* deterministic — boilerplate and uncheckable criteria, found in code;
* model — stated details the tickets dropped, open questions the input
  already answers, Sub-tasks that trace to no criterion. Every one must QUOTE
  the requirement (or name a real Sub-task); a finding that cannot be grounded
  is discarded, so the reviewer of the draft cannot over-reach either.

The caller regenerates once for AVOIDABLE findings, then ships with whatever
remains named in the reply. It never invents: a detail that cannot be filled
from context already gathered is not AVOIDABLE — it is asked.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from common_lib.utils.logger import setup_logger

from src.models.schemas import WorkBreakdown
from src.shared.rubric import BY_CODE, Avoidability, boilerplate_in

logger = setup_logger(__name__)

Chat = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class SelfFinding:
    code: str
    detail: str
    quote: str = ""

    @property
    def avoidable(self) -> bool:
        return BY_CODE[self.code].avoidability is Avoidability.AVOIDABLE

    def as_instruction(self) -> str:
        if self.code == "STATED_DETAIL_MISSING":
            return f'The requirement states "{self.quote}" but no Story or Sub-task carries it.'
        if self.code == "STATED_EDGE_CASE_MISSING":
            return f'The requirement describes "{self.quote}" but no criterion covers it.'
        if self.code == "ASKED_WHAT_WAS_STATED":
            return f'Open question "{self.detail}" is answered by "{self.quote}" — remove it.'
        if self.code == "NO_ACTOR":
            return (
                f'Story "{self.detail}" is written "As a system" or "As a user" — it '
                "should name the person it serves."
            )
        if self.code == "UNTRACED_SUBTASK":
            return f'Sub-task "{self.detail}" delivers no acceptance criterion of its Story.'
        return f"{BY_CODE[self.code].meaning}: {self.detail}"


# "As a system" — and "As a billing system" (BGV-27, BGV-30, 2026-09-25), which
# an exact match let through. Any machine as the "user" of a user story.
_NO_PERSON = re.compile(
    r"as an? (?:user|person|(?:[\w-]+\s+){0,2}"
    # The machine word must END the role: "As a system administrator" is a person.
    r"(?:system|service|platform|application|app|tool|bot|process|job|engine))\s*,",
    re.IGNORECASE,
)


def deterministic(breakdown: WorkBreakdown) -> list[SelfFinding]:
    """What code can see: boilerplate, and a Story with no actor."""
    found: list[SelfFinding] = []
    for story in breakdown.stories:
        texts = [story.description, *story.acceptance_criteria]
        for sub in story.subtasks:
            texts += [sub.description, sub.expected_outcome, sub.completion_criteria]
        for text in texts:
            phrase = boilerplate_in(text)
            if phrase:
                found.append(SelfFinding("BOILERPLATE", f'"{phrase}" in {story.title}'))
        # "As a user" names nobody; the requirement's own role must be used.
        if _NO_PERSON.match(story.user_story_statement.strip()):
            found.append(SelfFinding("NO_ACTOR", story.title))
    return found


_PROMPT = """\
Check this proposed Jira breakdown against the requirement it was made from.
Report ONLY these, and only when the requirement text proves them:

1. "stated_missing": a detail the REQUIREMENT states — a number, role, status,
   rule or limit — that appears in NO Story or Sub-task. Give its exact words
   from the requirement as "quote".
2. "edge_missing": an error or edge case the REQUIREMENT describes that no
   acceptance criterion covers. Quote it exactly.
3. "asked_but_stated": an open question the REQUIREMENT already answers. Give
   the question exactly as written, and quote the answer.
4. "untraced": Sub-task titles that deliver no acceptance criterion of their
   Story.

Never report something the requirement does not state — a missing detail the
requirement never mentions is NOT a finding here. Empty lists are correct when
the breakdown is faithful.

Return JSON only:
{{"stated_missing": [{{"quote": "..."}}], "edge_missing": [{{"quote": "..."}}],
  "asked_but_stated": [{{"question": "...", "quote": "..."}}], "untraced": ["..."]}}

REQUIREMENT
{requirement}

BREAKDOWN
{breakdown}
"""


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


async def model_findings(
    requirement: str, breakdown: WorkBreakdown, chat: Chat
) -> list[SelfFinding]:
    """The model's findings, each grounded or discarded."""
    from src.classification.decompose import _parse_json_object  # no import cycle

    raw = await chat(
        _PROMPT.format(
            requirement=requirement,
            breakdown=json.dumps(breakdown.model_dump(mode="json"), indent=1)[:12000],
        )
    )
    data = _parse_json_object(raw)
    source = _flat(requirement)
    questions = {_flat(q): q for s in breakdown.stories for q in s.open_questions}
    subtasks = {_flat(t.title): t.title for s in breakdown.stories for t in s.subtasks}

    kept: list[SelfFinding] = []
    dropped = 0

    def grounded(quote: str) -> bool:
        q = _flat(quote)
        return len(q) >= 3 and q in source

    for item in data.get("stated_missing") or []:
        quote = str((item or {}).get("quote") or "")
        if grounded(quote):
            kept.append(SelfFinding("STATED_DETAIL_MISSING", quote, quote))
        else:
            dropped += 1
    for item in data.get("edge_missing") or []:
        quote = str((item or {}).get("quote") or "")
        if grounded(quote):
            kept.append(SelfFinding("STATED_EDGE_CASE_MISSING", quote, quote))
        else:
            dropped += 1
    for item in data.get("asked_but_stated") or []:
        question = questions.get(_flat(str((item or {}).get("question") or "")))
        quote = str((item or {}).get("quote") or "")
        if question and grounded(quote):
            kept.append(SelfFinding("ASKED_WHAT_WAS_STATED", question, quote))
        else:
            dropped += 1
    for title in data.get("untraced") or []:
        real = subtasks.get(_flat(str(title)))
        if real:
            kept.append(SelfFinding("UNTRACED_SUBTASK", real))
        else:
            dropped += 1
    if dropped:
        logger.info(f"self-review: discarded {dropped} ungrounded finding(s)")
    return kept


async def review_draft(requirement: str, breakdown: WorkBreakdown, chat: Chat) -> list[SelfFinding]:
    """Deterministic plus model findings. A model failure leaves the former."""
    found = deterministic(breakdown)
    try:
        found += await model_findings(requirement, breakdown, chat)
    except Exception as exc:  # noqa: BLE001 — the gate must not fail the build
        logger.info(f"self-review model check unavailable: {exc}")
    logger.info(
        f"self-review: {len(found)} finding(s) — "
        + (", ".join(sorted({f.code for f in found})) or "none")
    )
    return found


def drop_answered_questions(breakdown: WorkBreakdown, findings: list[SelfFinding]) -> WorkBreakdown:
    """Remove open questions the input already answers — asking them is a mistake."""
    answered = {_flat(f.detail) for f in findings if f.code == "ASKED_WHAT_WAS_STATED"}
    if not answered:
        return breakdown
    data = breakdown.model_dump(mode="json")
    for story in data["stories"]:
        story["open_questions"] = [
            q for q in story.get("open_questions") or [] if _flat(q) not in answered
        ]
    return WorkBreakdown.model_validate(data)
