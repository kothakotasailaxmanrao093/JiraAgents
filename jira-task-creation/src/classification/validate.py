"""Deterministic input validation for the work-breakdown agent.

This module is the first gate in the pipeline and it never calls Jira or an
LLM. Everything decidable by rule is decided here, so the obvious rejections
(empty, gibberish, small talk) cost nothing and cannot be talked out of by a
model. Cases that genuinely need judgement are handed on to the LLM triage in
``decomposer.py``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from src.models.schemas import ResultStatus, ValidationVerdict

MAX_REQUIREMENT_CHARS = 20_000

EMPTY_MESSAGE = (
    "Please provide a project, feature, or business requirement before " "creating Jira work items."
)
MEANINGLESS_MESSAGE = (
    "The text provided does not contain a usable requirement. Please describe "
    "the project, feature, or business capability you need, including what "
    "should change and for whom."
)
OUT_OF_SCOPE_MESSAGE = (
    "This request is outside the scope of the work-breakdown agent, which decomposes "
    "project, product, and business requirements into Jira work items. No Jira "
    "issues were created."
)

# A requirement is expected to talk about doing something to something. These
# verbs cover the overwhelming majority of real requirement phrasings; their
# total absence is a strong signal, but on its own it only downgrades to
# "needs a closer look" rather than an outright rejection.
_ACTION_WORDS = frozenset(
    """
    add allow build capture change configure create customise customize deliver
    display enable enforce enhance ensure expose extend generate handle
    implement improve integrate introduce let maintain manage migrate modify
    notify offer optimise optimize provide publish record redesign reduce
    refactor remove replace report require restrict retire schedule send show
    store support sync synchronise synchronize track update upgrade validate
    verify view need should must want able
    """.split()
    # Added after a live run dropped "alert the depot manager when a fault is
    # reported" as non-actionable: it is eight words with no verb the list
    # recognised, so the length rule discarded a real capability.
    + """
    accept alert approve archive assign attach calculate cancel close compare
    complete decline delete download escalate export filter flag generate
    import invite list mark message print reassign refund reject reopen renew
    reset resolve restore review search select share sort submit tag transfer
    unassign upload see edit save open close start stop pause resume assign
    """.split()
    # Added after "Log a rest break." was refused as "does not describe any
    # work to be done": "log" is one of the commonest verbs in a product
    # requirement and was simply absent, so a perfectly good request was
    # rejected while "Record a rest break." was accepted. These are the rest of
    # the everyday product verbs the list had never covered.
    #
    # Deliberately NOT added: "order" (matches "in order to"), "plan",
    # "document", "group" and "design", which are used as nouns far more often
    # than as verbs and would let vague text past the gate.
    + """
    log sign register authenticate subscribe unsubscribe monitor audit remind
    route dispatch clone duplicate merge split link unlink rate confirm
    activate deactivate disable block suspend grant revoke convert translate
    forecast estimate allocate reserve book invoice scan measure classify
    categorise categorize collect rename deploy release launch trigger
    """.split()
)


def _verb_stem(word: str) -> str:
    """Crude stem so "tracking"/"tracks"/"tracked" match "track"."""
    w = word.lower()
    for suffix, cut in (("ing", 3), ("ed", 2), ("es", 2), ("s", 1)):
        if len(w) > cut + 2 and w.endswith(suffix):
            stem = w[:-cut]
            if stem in _ACTION_WORDS:
                return stem
            # "completed" cuts to "complet", which is not a word. The dropped
            # "e" has to be put back, exactly as it already was for "-ing" —
            # without it, "completed", "created" and "updated" matched nothing.
            if suffix in ("ing", "ed", "es") and stem + "e" in _ACTION_WORDS:
                return stem + "e"
    return w


# Conversational openers that are meaningful English but carry no work intent.
_SMALL_TALK_PATTERNS = (
    r"^\s*(hi|hey|hello|yo|good\s+(morning|afternoon|evening))\b[\s\W]*$",
    r"\bhow\s+are\s+you\b",
    r"\bwhat(?:'s| is)\s+the\s+weather\b",
    r"\bwhat\s+time\s+is\s+it\b",
    r"\btell\s+me\s+a\s+joke\b",
    r"\bwho\s+are\s+you\b",
    r"^\s*(thanks|thank\s+you|ok|okay|cool|nice)\b[\s\W]*$",
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_VOWEL_RE = re.compile(r"[aeiou]")


@dataclass(frozen=True)
class ProjectContext:
    """Configured project/domain context used for relevance checks.

    Nothing here is hardcoded to a business domain: every value comes from the
    environment or the trigger payload, and an unset value simply means the
    corresponding check is not applied.
    """

    name: str = ""
    description: str = ""
    allowed_domains: tuple[str, ...] = ()
    jira_project_key: str = ""
    existing_titles: tuple[str, ...] = ()

    @classmethod
    def from_jira(
        cls,
        project: dict[str, object] | None,
        existing: list[dict[str, object]] | None = None,
        name_override: str = "",
    ) -> ProjectContext:
        """Build context from what Jira actually holds, not from a typed form.

        The project's own description is the authoritative statement of what the
        project is for, so relevance is judged against that rather than against
        whatever someone remembered to paste into a field.
        """
        project = project or {}
        # "KEY — Title", not the bare title: a clarifying question asking
        # "does this differ from <existing ticket>?" is useless without a key
        # the reader can actually open. The model can only cite what it is
        # given, and it was never given a key before this.
        titles = [
            f"{i.get('key', '')} — {i.get('summary', '')}".strip(" —")
            for i in (existing or [])
            if str(i.get("summary", "")).strip()
        ]
        return cls(
            name=(name_override or str(project.get("name") or "")).strip(),
            description=str(project.get("description") or "").strip(),
            allowed_domains=(),
            jira_project_key=str(project.get("key") or "").strip().upper(),
            existing_titles=tuple(titles[:60]),
        )

    @classmethod
    def from_env(cls, overrides: dict[str, str] | None = None) -> ProjectContext:
        over = overrides or {}
        raw_domains = over.get("allowed_domains") or os.environ.get("LTW_ALLOWED_DOMAINS", "")
        domains = tuple(d.strip() for d in raw_domains.split(",") if d.strip())
        return cls(
            name=(over.get("project_name") or os.environ.get("LTW_PROJECT_NAME", "")).strip(),
            description=(
                over.get("project_description") or os.environ.get("LTW_PROJECT_DESCRIPTION", "")
            ).strip(),
            allowed_domains=domains,
            jira_project_key=(
                over.get("jira_project_key") or os.environ.get("JIRA_PROJECT_KEY", "")
            )
            .strip()
            .upper(),
        )

    def as_prompt_text(self, *, for_breakdown: bool = False) -> str:
        """Render the context for the LLM. Says so explicitly when unconfigured."""
        if not (self.name or self.description or self.allowed_domains):
            return (
                "No project context is configured. Judge relevance only on whether "
                "the text is a project, product, software, or business requirement."
            )
        lines = []
        if self.name:
            lines.append(f"Project name: {self.name}")
        if self.description:
            lines.append(f"Project description: {self.description}")
        if self.allowed_domains:
            lines.append(f"In-scope domains/modules: {', '.join(self.allowed_domains)}")
        if self.jira_project_key:
            lines.append(f"Jira project key: {self.jira_project_key}")
        # Existing tickets go to the BREAKDOWN only, as context. Triage used to
        # get them with "say so if the requirement repeats one", and did: on
        # fresh BGV-15 it asked "does this differ from BGV-2 or BGV-3?" instead
        # of letting the duplicate check answer "this work already exists"
        # with the keys (2026-09-25). Whether work repeats is decided after the
        # breakdown, deterministically and by adjudication — never at triage.
        if self.existing_titles and for_breakdown:
            listed = "\n".join(f"  - {t}" for t in self.existing_titles[:40])
            lines.append(
                "Tickets that already exist in this project (context only — "
                f"do not propose stories that repeat them):\n{listed}"
            )
        return "\n".join(lines)

    @property
    def is_configured(self) -> bool:
        return bool(self.name or self.description or self.allowed_domains)


def normalise(text: object) -> str:
    """Coerce any trigger value to a trimmed string. ``None`` becomes ``""``."""
    if text is None:
        return ""
    if isinstance(text, str):
        return text.strip()
    return str(text).strip()


# The minimum number of real words below which text is treated as unusable
# rather than merely under-specified.
_MIN_WORDS_FOR_CLARIFICATION = 6


def _word_like_count(text: str) -> tuple[int, int]:
    """Return ``(word_like, total)`` token counts for ``text``.

    A token counts as word-like when it contains a vowel, is not an obvious
    keyboard run, and is not implausibly long.
    """
    words = _WORD_RE.findall(text.lower())
    keyboard_runs = ("asdf", "qwer", "zxcv", "hjkl", "uiop", "wasd", "qwty")
    word_like = 0
    for word in words:
        if any(run in word for run in keyboard_runs):
            continue
        if len(word) > 2 and not _VOWEL_RE.search(word):
            continue
        if len(word) > 15:
            continue
        word_like += 1
    return word_like, len(words)


def _looks_like_gibberish(text: str) -> bool:
    """True when the text is keyboard mash rather than language."""
    word_like, total = _word_like_count(text)
    if total == 0:
        # No alphabetic content at all — digits, punctuation or symbols only.
        return True
    return word_like < max(2, total // 2)


def _is_repetitive(text: str) -> bool:
    """True for input that is one token repeated (``hello hello hello``)."""
    words = [w for w in text.lower().split() if w]
    return len(words) >= 2 and len(set(words)) == 1


def _is_small_talk(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in _SMALL_TALK_PATTERNS)


# Words that express a wish rather than an action. On their own they name
# nothing to build: "I am feeling hungry and I want to eat biryani" matched on
# "want" and produced a Story with that sentence as its title. They stay in
# _ACTION_WORDS because "users should be able to reset their password" is a
# real requirement — but something else in the sentence has to carry the work.
_WISH_WORDS = frozenset({"need", "should", "must", "want", "able"})


def has_action_intent(text: str) -> bool:
    """True when the text names something to be done.

    A wish word alone is not enough. "I want to eat biryani" and "we need a
    decision" say what someone would like, not what should be built.
    """
    words = _WORD_RE.findall(text.lower())
    matched = {
        w if w in _ACTION_WORDS else _verb_stem(w)
        for w in words
        if w in _ACTION_WORDS or _verb_stem(w) in _ACTION_WORDS
    }
    return bool(matched - _WISH_WORDS)


def starts_with_verb(text: str) -> bool:
    """True when the first word is an action verb.

    This is what separates a new capability from a continuation. In "show the
    pickup and drop-off addresses" the fragment after "and" begins with a noun,
    so it belongs to the same capability; in "... and alert the manager" it
    begins with a verb, so it is a second one.
    """
    words = _WORD_RE.findall(text.strip())
    if not words:
        return False
    first = words[0].lower()
    return first in _ACTION_WORDS or _verb_stem(first) in _ACTION_WORDS


def validate_input(raw: object, context: ProjectContext | None = None) -> ValidationVerdict:
    """Run the deterministic gate over the incoming requirement.

    Returns ``READY_FOR_JIRA`` only in the sense of "cleared this gate" — the
    LLM triage and the schema validation still stand between here and any Jira
    write.
    """
    text = normalise(raw)

    # The "(no description provided)" marker is written in when the person said
    # nothing. It is not a requirement, and it reads as one — "provided" stems
    # to the action word "provide" — so a comment of just "@Aetherion" passed
    # this gate and went on to be decomposed.
    from src.context.ingest import NO_REQUIREMENT_STATED

    if text.strip() == NO_REQUIREMENT_STATED:
        text = ""

    if not text:
        return ValidationVerdict(
            status=ResultStatus.VALIDATION_ERROR,
            reason=EMPTY_MESSAGE,
            validation_errors=["Requirement description is empty."],
        )

    if len(text) > MAX_REQUIREMENT_CHARS:
        return ValidationVerdict(
            status=ResultStatus.VALIDATION_ERROR,
            reason=(
                f"The requirement is longer than the supported "
                f"{MAX_REQUIREMENT_CHARS} characters. Please summarise it."
            ),
            validation_errors=[f"Requirement exceeds {MAX_REQUIREMENT_CHARS} characters."],
        )

    if len(text) < 10 or len(_WORD_RE.findall(text)) < 3:
        return ValidationVerdict(
            status=ResultStatus.VALIDATION_ERROR,
            reason=MEANINGLESS_MESSAGE,
            validation_errors=["Requirement is too short to describe any work."],
        )

    if _is_repetitive(text) or _looks_like_gibberish(text):
        return ValidationVerdict(
            status=ResultStatus.VALIDATION_ERROR,
            reason=MEANINGLESS_MESSAGE,
            validation_errors=["Requirement does not contain readable requirement text."],
        )

    if _is_small_talk(text) and not has_action_intent(text):
        return ValidationVerdict(
            status=ResultStatus.OUT_OF_SCOPE,
            reason=OUT_OF_SCOPE_MESSAGE,
        )

    if not has_action_intent(text):
        word_like, _ = _word_like_count(text)
        if word_like < _MIN_WORDS_FOR_CLARIFICATION:
            # Too little real language to be an under-specified requirement;
            # there is nothing here to ask a clarifying question about.
            return ValidationVerdict(
                status=ResultStatus.VALIDATION_ERROR,
                reason=MEANINGLESS_MESSAGE,
                validation_errors=["Requirement does not describe any work to be done."],
            )
        # Readable, but nothing is being asked for. Ask rather than reject: the
        # user may simply have described a problem without stating the change.
        ctx = context or ProjectContext()
        questions = ["What change or capability should be delivered for this requirement?"]
        if ctx.is_configured:
            questions.append("Which project or module should this requirement belong to?")
        return ValidationVerdict(
            status=ResultStatus.CLARIFICATION_REQUIRED,
            reason="The text does not state what should be built or changed.",
            clarifying_questions=questions,
        )

    return ValidationVerdict(status=ResultStatus.READY_FOR_JIRA)
