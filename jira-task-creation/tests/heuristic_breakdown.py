"""The test stand-in for the model's work breakdown (moved out of src, 2026-10-22).

Production never builds a breakdown without the model: when the model fails,
nothing is created (owner decision, 2026-09-25). The tests still need a
breakdown that follows the requirement's wording without a model call, so this
deterministic writer lives here, with the helpers only it uses. It restates the
capabilities the requirement asks for and never invents business rules, roles
or dates.
"""

from __future__ import annotations

import re

from common_lib.utils.logger import setup_logger

from src.classification.decompose import (
    _USER_STORY_RE,
    split_capabilities,
    strip_section_headings,
)
from src.classification.validate import ProjectContext, has_action_intent, starts_with_verb
from src.context.ingest import requirement_core
from src.models.schemas import (
    NO_DEPENDENCIES,
    NOT_SPECIFIED,
    Classification,
    Complexity,
    Epic,
    Priority,
    Story,
    Subtask,
    WorkBreakdown,
)

logger = setup_logger(__name__)
# Jira caps every summary at 255. One constant, used everywhere.
MAX_SUMMARY_CHARS = 255


# Requester framing: how someone asks for a thing, not the thing itself.
_REQUESTER_FRAMING = (
    "we would like to",
    "we would like",
    "i would like to",
    "i would like",
    "it would be good if",
    "it would be nice if",
    "the client wants to",
    "the client wants",
    "the customer wants to",
    "the customer wants",
    "we want to",
    "we want",
    "can we please",
    "can we",
    "could we",
    "please could you",
    "please can you",
    "please",
)


# Modal padding that adds no meaning to an imperative phrase.
_MODAL_PADDING = (
    "should be able to",
    "must be able to",
    "needs to be able to",
    "need to be able to",
    "to be able to",
    "should be",
    "must be",
    "has to",
    "have to",
    "needs to",
    "need to",
)


# Verbs that introduce an actor: "allow tenants to X" → actor tenants, action X.
_ACTOR_VERBS = ("allow", "allows", "let", "lets", "enable", "enables", "permit", "permits")


# "let them view ..." names no actor — the pronoun has no antecedent here, so
# it must not be carried into the story as if it were one.
_PRONOUN_ACTORS = frozenset(
    "them they it he she us we you me him her someone anyone everyone".split()
)


_GENERIC_ACTOR = "user of this product"


# An actor is a short noun phrase. Anything longer, or containing a verb, is
# the start of the action — not the actor.
_MAX_ACTOR_WORDS = 3


_ARTICLES = ("a ", "an ", "the ")


_FALLBACK_TITLE = "Requirement"


# Benefit phrasing keyed off the verb. Still generic — only the model can state
# real business value — but it beats one fixed sentence on every story.
_BENEFIT_BY_VERB = {
    "see": "I have the information in front of me when I need it",
    "view": "I have the information in front of me when I need it",
    "track": "I can see the current position without asking anyone",
    "export": "I can work with the data outside this system",
    "alert": "the right person hears about it without being chased",
    "notify": "the right person hears about it without being chased",
    "send": "the message reaches the recipient without manual effort",
    "assign": "work reaches the right person without a hand-off step",
    "report": "the problem is recorded the moment it is noticed",
    "update": "the record stays accurate without going through support",
    "upload": "the evidence is attached at the point it is captured",
    "download": "I keep a copy without asking anyone for it",
    "approve": "the decision is recorded and the work can move on",
    "cancel": "I can undo a mistake without contacting support",
    "filter": "I can find what I need in a long list",
    "mark": "the status reflects reality as soon as it changes",
}


_DEFAULT_BENEFIT = "I can do this myself instead of asking someone else"


def heuristic_breakdown(requirement: str, context: ProjectContext) -> WorkBreakdown:
    """Build a schema-valid breakdown without a model.

    Faithful but generic: it restates the capabilities the requirement asks for
    and never invents business rules, roles, or dates.
    """
    # Everything below turns prose into ticket wording. The model can be told in
    # a heading that comments are only context; this path cannot read that, so it
    # is given the requirement sections alone, with the headings removed.
    requirement, exclusions = extract_exclusions(requirement_core(requirement))
    requirement = strip_section_headings(requirement)
    capabilities, dropped = drop_excluded(split_capabilities(requirement), exclusions)
    if dropped:
        logger.info(f"Ruled out in discussion, so not built: {dropped}")
    # Size what is actually being built. Sizing the original text counted work
    # that was ruled out, so excluding two of three capabilities still produced
    # an Epic over a single Story.
    #
    # Only the tests use this writer (a stand-in for the model). It cannot tell
    # parts of one feature from separate features, so it never says Medium (one
    # Story): one capability is Small, several are Large — an Epic with a Story
    # per capability (sizes as of the owner decision, 2026-10-07).
    classification = Classification.SMALL if len(capabilities) <= 1 else Classification.LARGE
    # Carry the most recently named actor across clauses that name none.
    stories: list[Story] = []
    seen_actor = ""
    for cap in capabilities:
        story = _heuristic_story(cap, seen_actor)
        named, _ = extract_actor(cap)
        if named != _GENERIC_ACTOR:
            seen_actor = named
        stories.append(story)

    if classification is Classification.SMALL:
        return WorkBreakdown(
            classification=classification,
            analysis=(
                "The requirement asks for one focused change, so it is sized as "
                "Small and produces a single Task with its subtasks."
            ),
            stories=stories[:1],
        )
    scope_label = context.name or "the configured project"
    # Built from the capabilities that survived, not from the original text. An
    # Epic titled "Send email and SMS notifications" with no SMS Story under it
    # tells a reader the work is covered when it was deliberately dropped.
    epic_text = "; ".join(capabilities) if dropped else requirement.strip()
    epic = Epic(
        business_objective=(
            f"Deliver the capabilities described in the requirement for {scope_label}: "
            f"{truncate(epic_text, 400)}"
        ),
        scope=capabilities,
        # What was ruled out is known here, and this is the field for it.
        out_of_scope=dropped or [NOT_SPECIFIED],
        priority=Priority.MEDIUM,
        acceptance_criteria=[
            "Every capability listed in scope is delivered and demonstrable.",
            "Each related user story has passed its own acceptance criteria.",
        ],
        jira_summary=_titleise(epic_text) or "Requirement decomposition",
    )
    return WorkBreakdown(
        classification=classification,
        analysis=(
            f"The requirement asks for {len(capabilities)} distinct capabilities, "
            f"so it is sized as {classification.value} and produces an Epic with "
            f"one Story per capability."
        ),
        epic=epic,
        stories=stories,
    )


def _heuristic_story(capability: str, fallback_actor: str = "") -> Story:
    raw_actor, raw_action = extract_actor(capability)
    # A list usually names its actor once: "Let dispatchers assign a job, and
    # notify the dispatcher when it is declined." The second clause has no
    # actor of its own, and a reader carries the first one across. Do the same
    # before giving up on "user of this product".
    actor = fallback_actor if raw_actor == _GENERIC_ACTOR and fallback_actor else raw_actor
    action = to_infinitive(raw_action)

    # "I want to <action>" only reads as English when <action> is a verb
    # phrase. When it is not — "the tyre log needs a monthly summary" — the
    # template produced "As a user of this product, I want to the tyre log
    # needs a monthly summary", which is what a reader sees and judges the
    # whole agent by. State the capability instead of forcing it into a shape
    # it does not fit.
    phrasable = bool(action) and starts_with_verb(action)
    if phrasable:
        statement = (
            f"As {_article_for(actor)} {actor}, I want to {action}, so that {_benefit(action)}."
        )
        value = (
            f"Gives the {actor} direct access to {action}, which the requirement "
            f"asks for explicitly."
        )
    else:
        subject = action or capability
        # The schema requires "I want" and "so that", so the shape is fixed —
        # what changes is that the requirement is quoted as a statement rather
        # than jammed in after "I want to".
        # The schema requires "I want to <action>" or "I want a/the <thing>",
        # so the capability is quoted as a thing rather than jammed in after
        # "I want to".
        statement = (
            f"As {_article_for(actor)} {actor}, I want the capability described "
            f'as "{subject}", so that the stated outcome is delivered.'
        )
        value = f"Delivers what the requirement states for the {actor}: {subject}."

    return Story(
        title=_titleise(action or capability),
        user_story_statement=statement,
        description=(f"Deliver the capability described in the requirement as: {capability}."),
        business_value=value,
        priority=Priority.MEDIUM,
        estimated_complexity=Complexity.MEDIUM,
        dependencies=NO_DEPENDENCIES,
        acceptance_criteria=[
            f'The behaviour described as "{capability}" is available to the intended users.',
            "The behaviour matches the rules agreed with the product owner.",
            "Error and edge cases behave as agreed rather than failing silently.",
        ],
        subtasks=_heuristic_subtasks(capability),
    )


def extract_exclusions(text: str) -> tuple[str, list[str]]:
    """Split the "ruled out" section off the requirement.

    Returns ``(requirement_without_it, excluded_phrases)``. The section has to
    survive :func:`requirement_core` to get here — it is a decision about the
    work, not discussion — but it must never be decomposed, or "SMS" becomes a
    Story called "SMS".
    """
    from src.context.ingest import HEADING_EXCLUSIONS

    marker = f"## {HEADING_EXCLUSIONS}"
    if marker not in (text or ""):
        return text, []
    before, _, after = text.partition(marker)
    rest = re.split(r"(?m)^## ", after, maxsplit=1)
    body = rest[0]
    remainder = f"## {rest[1]}" if len(rest) > 1 else ""
    phrases = [
        line.lstrip("- ").strip().lower()
        for line in body.splitlines()
        if line.strip().startswith("-")
    ]
    return (before + remainder).strip(), [p for p in phrases if p]


def drop_excluded(capabilities: list[str], exclusions: list[str]) -> tuple[list[str], list[str]]:
    """Remove capabilities a comment ruled out. Returns ``(kept, dropped)``.

    Matching is word containment, not equality: a comment says "SMS", the
    capability reads "Send SMS notifications to customers". Every word of the
    exclusion must appear, so "email" does not knock out "email and SMS".
    """
    kept: list[str] = []
    dropped: list[str] = []
    for capability in capabilities:
        words = set(re.findall(r"[a-z0-9]+", capability.lower()))
        if any(
            phrase and set(re.findall(r"[a-z0-9]+", phrase)).issubset(words)
            for phrase in exclusions
        ):
            dropped.append(capability)
        else:
            kept.append(capability)
    # Never empty the breakdown: if everything matched, the exclusion was too
    # broad to trust and a human should decide rather than get nothing.
    return (kept, dropped) if kept else (capabilities, [])


def extract_actor(clause: str) -> tuple[str, str]:
    """Split ``allow tenants to X`` into ``("tenants", "X")``.

    Returns ``("user of this product", <clause>)`` when no actor is stated, so
    the caller always has something to put in "As a ...".
    """
    text = normalise_capability(clause)
    story = _USER_STORY_RE.match(text)
    if story:
        actor = re.match(r"^\s*as an?\s+(.{2,40}?),", text, re.IGNORECASE).group(1)
        action = re.split(r",\s*i\s+want\s*", text, maxsplit=1, flags=re.IGNORECASE)[1]
        action = re.split(r",\s*so that\b", action, maxsplit=1, flags=re.IGNORECASE)[0]
        return _singular(_clean_actor(actor)), to_infinitive(action.strip())
    lowered = text.lower()
    for verb in _ACTOR_VERBS:
        if lowered.startswith(verb + " "):
            rest = text[len(verb) :].strip()
            # "tenants to download a receipt" → actor "tenants", action "download..."
            match = re.match(r"(.{2,40}?)\s+to\s+(.+)", rest, re.IGNORECASE)
            if match:
                actor = match.group(1).strip(" ,")
                action = match.group(2).strip()
                if actor and action and _plausible_actor(actor):
                    if actor.lower() in _PRONOUN_ACTORS:
                        return _GENERIC_ACTOR, action
                    return _singular(_clean_actor(actor)), action
            # Either no "to", or the text before it was not an actor:
            # "dispatchers assign a job to a driver", "a driver report a fault".
            return _split_first_noun(rest)
    # "every tenant update their contact number"
    match = re.match(r"(every|each|any|all)\s+(\w+)\s+(.+)", text, re.IGNORECASE)
    if match:
        return _singular(_clean_actor(match.group(2))), match.group(3).strip()
    # "operations managers could see the total fuel cost ..." — a modal names
    # the actor just as clearly as "allow <role> to ...", and the modal itself
    # carries no meaning once the action is stated imperatively.
    match = re.match(
        r"(.{2,40}?)\s+(?:could|can|should|must|may|will|would)\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if match:
        actor = match.group(1).strip(" ,")
        action = match.group(2).strip()
        if action and _plausible_actor(actor):
            if actor.lower() in _PRONOUN_ACTORS:
                return _GENERIC_ACTOR, action
            return _singular(_clean_actor(actor)), action
    return _GENERIC_ACTOR, text


def _heuristic_subtasks(capability: str) -> list[Subtask]:
    """Three delivery-shaped subtasks per capability, described by behaviour."""
    return [
        Subtask(
            title=truncate(f"Define the rules for: {_titleise(capability)}"),
            description=(
                f"Agree and record the functional rules and edge cases for "
                f'"{capability}" with the product owner, using only what the '
                f"requirement states."
            ),
            expected_outcome=(
                "An agreed, written set of rules and edge cases that the "
                "implementation and tests can both be checked against."
            ),
            dependencies=NO_DEPENDENCIES,
            completion_criteria=(
                "The product owner has confirmed the recorded rules cover the "
                "capability with no open questions."
            ),
        ),
        Subtask(
            title=truncate(f"Implement the behaviour for: {_titleise(capability)}"),
            description=(
                f'Build the behaviour described by "{capability}" according to '
                f"the agreed rules."
            ),
            expected_outcome=(
                "The capability behaves as described for the stated users under "
                "normal and error conditions."
            ),
            dependencies="Depends on the agreed rules for this capability.",
            completion_criteria=(
                "The behaviour is demonstrable end to end and matches every " "agreed rule."
            ),
        ),
        Subtask(
            title=truncate(f"Verify and hand over: {_titleise(capability)}"),
            description=(
                f'Validate "{capability}" against the story acceptance criteria '
                f"and hand the capability over for review."
            ),
            expected_outcome=(
                "Evidence that each acceptance criterion for this capability is "
                "met, ready for sign-off."
            ),
            dependencies="Depends on the implemented behaviour for this capability.",
            completion_criteria=("All acceptance criteria pass and the reviewer has signed off."),
        ),
    ]


def _article_for(noun: str) -> str:
    """ "a" or "an", chosen by how the word is said rather than how it is spelt.

    Plain vowel-letter matching gets the common fleet actors wrong both ways:
    "an operations manager" is right, but so is "a user" — and "user" starts
    with a vowel letter.
    """
    word = (noun or "").strip().split()[0].lower() if (noun or "").strip() else ""
    if not word:
        return "a"
    # Sounded as a consonant despite the vowel letter: user, unit, European…
    if word.startswith(("eu", "uni", "use", "user", "usu", "one")):
        return "a"
    # Sounded as a vowel despite the consonant letter: hour, honest…
    if word.startswith(("hour", "honest", "heir")):
        return "an"
    return "an" if word[0] in "aeiou" else "a"


def _benefit(action: str) -> str:
    first = action.strip().split()[0].lower() if action.strip() else ""
    return _BENEFIT_BY_VERB.get(first, _DEFAULT_BENEFIT)


def _plausible_actor(candidate: str) -> bool:
    """True when ``candidate`` reads as a person/role rather than a phrase.

    Guards the ``to``-split below. In "dispatchers assign a job to a driver"
    the first " to " is the indirect object, so a naive split hands back the
    actor "dispatchers assign a job" and the action "a driver". Requiring the
    actor to be short and verb-free rejects that and falls through to the
    first-noun rule instead.
    """
    words = candidate.split()
    if not words or len(words) > _MAX_ACTOR_WORDS:
        return False
    return not has_action_intent(candidate)


def _split_first_noun(rest: str) -> tuple[str, str]:
    """``a driver report a fault`` → ``("driver", "report a fault")``."""
    text = rest.strip()
    lowered = text.lower()
    for article in _ARTICLES:
        if lowered.startswith(article):
            text = text[len(article) :].strip()
            break
    words = text.split()
    if len(words) < 2:
        return _GENERIC_ACTOR, rest
    if words[0].lower() in _PRONOUN_ACTORS:
        return _GENERIC_ACTOR, " ".join(words[1:]).strip()
    return _singular(_clean_actor(words[0])), " ".join(words[1:]).strip()


def to_infinitive(action: str) -> str:
    """Make the action read correctly after "I want to ...".

    Strips a leading "to" so it is never doubled, and lowercases the first
    letter so the sentence does not restart mid-clause.

    An acronym is left alone: lowercasing the first letter of "SMS
    notifications" produced Stories and user stories reading "sMS
    notifications".
    """
    text = re.sub(r"^to\s+", "", action.strip(), flags=re.IGNORECASE)
    if not text:
        return text
    first = text.split(maxsplit=1)[0]
    if len(first) > 1 and first[:2].isupper():
        return text
    return text[:1].lower() + text[1:]


def _titleise(clause: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """A capability phrase fit for a Jira summary.

    Normalises first, so requester framing ("We would like ...") and modal
    padding ("to be able to") never reach the title.
    """
    title = truncate(normalise_capability(_flatten_list_markers(clause)), limit)
    title = (title[:1].upper() + title[1:]) if title else ""
    # The schema demands at least 3 characters. Degenerate input ("to to to")
    # can normalise down to almost nothing, and a crash there would take down
    # a whole run over a nonsense requirement.
    return title if len(title) >= 3 else _FALLBACK_TITLE


def normalise_capability(clause: str) -> str:
    """Reduce a raw clause to a bare capability phrase.

    ``We would like every tenant to be able to update their contact number``
    becomes ``every tenant update their contact number``. Requester framing and
    modal padding say how the request was made, not what is being asked for.
    """
    text = _strip_leading(_deshout(clause.strip(" .;,-–—\t")), _REQUESTER_FRAMING)
    text = _strip_leading(text, _MODAL_PADDING)
    # Padding can also sit mid-phrase: "every tenant to be able to update ..."
    for phrase in _MODAL_PADDING:
        pattern = re.compile(rf"\s+{re.escape(phrase)}\s+", re.IGNORECASE)
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,:-")


def _clean_actor(actor: str) -> str:
    """Trim an article and normalise case: "an Operations Manager" → "operations manager"."""
    text = actor.strip(" ,")
    lowered = text.lower()
    for article in _ARTICLES:
        if lowered.startswith(article):
            text = text[len(article) :].strip()
            break
    # ALL CAPS input should not shout back out of the story statement.
    if text.isupper():
        text = text.lower()
    return text[:1].lower() + text[1:] if text else text


def _singular(actor: str) -> str:
    """ "tenants" → "tenant". Crude but right for the actor nouns that appear."""
    words = actor.split()
    if not words:
        return actor
    last = words[-1]
    if last.lower().endswith("ies") and len(last) > 4:
        words[-1] = last[:-3] + "y"
    elif last.lower().endswith("sses"):
        words[-1] = last[:-2]
    elif last.lower().endswith("s") and not last.lower().endswith(("ss", "us", "is")):
        words[-1] = last[:-1]
    return " ".join(words)


def truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Trim to ``limit`` on a word boundary, marking the cut with an ellipsis.

    Jira's own limit is 255 for every issue type. The previous code used three
    different magic numbers (80, 60, 120), none of them Jira's, and cut
    mid-word with nothing to show it had happened.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def _flatten_list_markers(text: str) -> str:
    """Turn a bullet list into a readable clause for a one-line summary.

    An Epic built from a bullet list used to be summarised
    "Add: - driver shift roster - fuel spend by depot - tyre replacement log",
    with the markers still in it, because a Jira summary is one line and the
    list had none of its own punctuation.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    items = [re.sub(r"^(?:[-*\u2022]|\d+[.)])\s*", "", line) for line in lines if line]
    if not items:
        return text
    if len(items) == 1:
        # A single bullet still has a marker that does not belong in a summary.
        return items[0]
    head, rest = items[0], [i for i in items[1:] if i]
    if not rest:
        return head
    joined = ", ".join(rest[:-1])
    tail = f"{joined} and {rest[-1]}" if joined else rest[-1]
    # "Please add:" already ends in a colon; do not add a second separator.
    return f"{head.rstrip(':')}: {tail}" if head.endswith(":") else f"{head}, {tail}"


def _strip_leading(text: str, phrases: tuple[str, ...]) -> str:
    """Remove any of ``phrases`` from the front of ``text``, repeatedly."""
    changed = True
    while changed:
        changed = False
        lowered = text.lower().lstrip()
        for phrase in phrases:
            if lowered.startswith(phrase):
                text = text.lstrip()[len(phrase) :].lstrip(" ,:-")
                changed = True
                break
    return text.strip()


def _deshout(text: str) -> str:
    """Turn ALL-CAPS prose into sentence case.

    A requirement typed in caps produced a shouting Jira title and a statement
    that read "I want to mARK A DELIVERY COMPLETE" — the first-letter lowercase
    applied to text that was already upper. Acronyms of four letters or fewer
    are left alone so CSV, PDF and GPS survive.
    """
    words = [w for w in text.split() if any(ch.isalpha() for ch in w)]
    if len(words) < 3:
        return text
    upper = [w for w in words if w.isupper()]
    # Judge the phrase, not each word. A per-word length exemption kept short
    # words like "MARK" upper, which then became "mARK" once the statement
    # lowercased the first letter. An occasional acronym in otherwise normal
    # prose ("as CSV and PDF") leaves this ratio well under the threshold.
    if len(upper) / len(words) < 0.6:
        return text
    return text.lower()
