"""Assemble one requirement text from everything read off a Jira issue.

Pure functions: no I/O, so the shaping rules stay unit-testable.

The output is deliberately sectioned and labelled. The description is the
stated requirement; comments, history and work logs are context around it. A
model handed an undifferentiated blob treats a passing remark in a comment as a
requirement, which is exactly the fabrication this agent must avoid — so each
section says what it is, and the prompt-facing headings mark discussion as
discussion.

Everything competes for one character budget. When it is exceeded, the
lowest-value sections are dropped whole rather than truncated mid-sentence: a
half-sentence of history is worse than no history.
"""

from __future__ import annotations

import os
import re

from common_lib.utils.logger import setup_logger

from src.jira.keywords import trigger_keyword
from src.models.schemas import SourceIssue

logger = setup_logger(__name__)

DEFAULT_MAX_CHARS = 24_000

# Lowest value first: this is the order sections are dropped in when the
# assembled text does not fit. The description is never dropped.
_DROP_ORDER = (
    "worklogs",
    "history",
    "activity",
    "links",
    "ticket",
    "comments",
    "attachments",
    # Dropped last of all: when a team links a Confluence page, that page is
    # usually the specification, so it outranks everything except the request.
    "confluence",
)


def max_context_chars() -> int:
    raw = os.environ.get("LTW_CONTEXT_MAX_CHARS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_CHARS
    except ValueError:
        logger.warning(
            f"LTW_CONTEXT_MAX_CHARS is not an integer ({raw!r}); using {DEFAULT_MAX_CHARS}"
        )
        value = DEFAULT_MAX_CHARS
    return max(1_000, value)


# A sentence naming the trigger keyword and telling the agent what to do is
# addressed to the agent, not a requirement. Left in, "Aetherion please break
# this down." is decomposed into a Story of its own.
_INSTRUCTION_WORDS = re.compile(
    r"\b(?:please|kindly|break\s+(?:this|it)\s+down|breakdown|decompose|split\s+(?:this|it)|"
    r"create\s+(?:the\s+)?(?:tickets?|stories|issues?|epic)|generate|analyse|analyze|"
    r"handle\s+(?:this|it)|take\s+care\s+of|can\s+you|help)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def strip_trigger_mentions(text: str, keyword: str) -> str:
    """Remove the trigger mention itself, keeping the requirement around it.

    People address the agent the way they address a colleague:
    ``@Aetherion Create a login feature for all users.`` The sentence *is* the
    requirement, so it must be kept — but the mention is not part of it, and
    left in it ends up in the Story title.

    Only unambiguous mentions are removed:

    * ``@Aetherion`` — the ``@`` makes it a mention, wherever it appears;
    * ``Aetherion,`` / ``Aetherion:`` / ``Aetherion -`` at the start of a
      sentence, where the punctuation marks direct address.

    A bare keyword used as the subject is left alone, so
    ``Aetherion must authenticate against the fleet API.`` keeps its subject.
    """
    if not text.strip() or not keyword:
        return text
    escaped = re.escape(keyword)

    # "@Aetherion" anywhere, with any trailing punctuation that belonged to it.
    text = re.sub(rf"@{escaped}\b[ \t]*[,:;-]?[ \t]*", "", text, flags=re.IGNORECASE)
    # "Aetherion," / "Aetherion:" / "Aetherion -" opening a sentence.
    text = re.sub(
        rf"(?:(?<=^)|(?<=[.!?])\s*)\s*{escaped}\b[ \t]*[,:;-][ \t]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def strip_agent_instructions(text: str, keyword: str) -> str:
    """Remove sentences that address the agent rather than state a requirement.

    A sentence is dropped only when it both names the trigger keyword and reads
    as an instruction, so "Aetherion please break this down." goes while
    "Aetherion must authenticate against the fleet API." stays. If every
    sentence would be dropped, the text is returned unchanged — better a noisy
    requirement than an empty one.
    """
    if not text.strip() or not keyword:
        return text
    mention = re.compile(rf"(?<![\w]){re.escape(keyword)}(?![\w])", re.IGNORECASE)

    kept, dropped = [], 0
    for sentence in _SENTENCE_SPLIT.split(text.strip()):
        if mention.search(sentence) and _INSTRUCTION_WORDS.search(sentence):
            dropped += 1
            continue
        kept.append(sentence)

    if not kept:
        # Every sentence was an instruction, so there is nothing else to use.
        # Keep the text, but still take the mention out of it.
        return strip_trigger_mentions(text, keyword)
    if dropped:
        logger.info(f"Dropped {dropped} instruction sentence(s) addressed to the agent")
    return strip_trigger_mentions(" ".join(kept).strip(), keyword)


# Section headings. Defined once so `requirement_core` recognises exactly what
# `build_sections` wrote.
HEADING_TICKET = "The ticket this was asked on (background, not the request)"
HEADING_ATTACHMENTS = "Attached documents (supporting material)"
HEADING_CONFLUENCE = "Linked Confluence pages (the specification, where one was linked)"
HEADING_EXCLUSIONS = "Ruled out in discussion (do not build these)"

# Written into the requirement when the person said nothing. It is a marker
# for a reader, never a requirement — anything judging the request has to
# recognise it, or the sentence itself gets validated as if it were the ask.
NO_REQUIREMENT_STATED = "(no description provided)"
HEADING_COMMENTS = "Discussion on the ticket (context, not necessarily requirements)"
HEADING_LINKS = "Linked work items (already exist — do not recreate)"
HEADING_HISTORY = "Recent field history (context only)"
HEADING_WORKLOGS = "Work logged so far (context only)"

# Sections that surround the requirement without stating it. The model is told
# as much in the headings themselves, but the heuristic fallback cannot read
# that nuance, so it is given the requirement sections only.
CONTEXT_HEADINGS = (
    HEADING_TICKET,
    HEADING_COMMENTS,
    HEADING_LINKS,
    HEADING_HISTORY,
    HEADING_WORKLOGS,
)


def requirement_core(text: str) -> str:
    """Drop the context sections, keeping only what states a requirement.

    Anything that turns prose into ticket wording must use this. Without it a
    comment like "[2026-09-01] Ann: nice to have" is decomposed into a Story of
    its own — which is exactly what happened on the first live run.

    Text that was never sectioned (a manual run) is returned unchanged.
    """
    if "## " not in (text or ""):
        return text
    blocks = re.split(r"(?m)^## ", text)
    kept = [block for block in blocks if block.strip() and not block.startswith(CONTEXT_HEADINGS)]
    if not kept:
        return text
    return "\n\n".join(f"## {block.strip()}" for block in kept)


# --- where the stories should land -----------------------------------------
#
# The requester says this in prose — "Add the stories to the current sprint." —
# so it has to be read out of the text and then REMOVED from it. Left in, the
# instruction is decomposed into a Story of its own, exactly like the agent
# instructions handled above.

_SPRINT_SENTENCE = re.compile(
    r"""(?:
          \b(?:add|put|place|move|include|schedule|assign)\b[^.!?]*
          \b(?:current|active|this|ongoing)\s+sprint\b
        | \bin(?:to)?\s+the\s+(?:current|active|ongoing)\s+sprint\b
        | \b(?:current|active)\s+sprint\b[^.!?]*\bplease\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_BACKLOG_SENTENCE = re.compile(
    r"\b(?:add|put|place|move|leave|keep)\b[^.!?]*\b(?:product\s+)?backlog\b",
    re.IGNORECASE,
)

PLACEMENT_BACKLOG = "backlog"
PLACEMENT_CURRENT_SPRINT = "current_sprint"


def detect_placement(text: str) -> tuple[str, str]:
    """Read the destination out of the request, and take the instruction out.

    Returns ``(placement, cleaned_text)``.

    * "Add the stories to the current sprint." -> ``current_sprint``
    * anything else, including saying nothing at all -> ``backlog``

    The sentence that carried the instruction is removed from the text, so
    "Add the stories to the current sprint" never becomes a Story. If removing
    it would empty the requirement, the text is left alone.
    """
    if not (text or "").strip():
        return PLACEMENT_BACKLOG, text

    placement = PLACEMENT_BACKLOG
    kept: list[str] = []

    for sentence in _SENTENCE_SPLIT.split(text.strip()):
        if _SPRINT_SENTENCE.search(sentence):
            placement = PLACEMENT_CURRENT_SPRINT
            continue
        if _BACKLOG_SENTENCE.search(sentence):
            # Explicit backlog is already the default; drop the instruction so
            # it is not mistaken for work.
            continue
        kept.append(sentence)

    if not kept:
        return placement, text

    cleaned = " ".join(kept).strip()
    if placement == PLACEMENT_CURRENT_SPRINT:
        logger.info("Requested placement: current sprint")
    return placement, cleaned


def _section(heading: str, body: str) -> str:
    return f"## {heading}\n{body.strip()}"


_URL_RE = re.compile(r"https?://\S+")


def strip_urls(text: str) -> str:
    """Remove bare URLs from a stated requirement.

    A link is a pointer to a requirement, never the requirement itself.
    Whatever is behind it arrives through the linked-pages section, or — when
    the page could not be read — does not arrive at all, which the reply says
    out loud. Leaving the URL in produced Jira Stories literally titled
    "Build what this spec says: https://…/wiki/spaces/FL/pages/131181/…".
    """
    without = _URL_RE.sub(" ", text or "")
    # A link on its own line leaves an empty bullet or a dangling colon behind.
    without = re.sub(r"[ \t]+", " ", without)
    without = re.sub(r"(?m)^[ \t]*[-*:]\s*$", "", without)
    without = re.sub(r"[:\-–—]\s*$", "", without.strip())
    return without.strip()


def strip_placement_instructions(text: str) -> str:
    """Remove a "put these in the current sprint" sentence, keeping the rest."""
    return detect_placement(text)[1]


# --- is this comment the requirement, or a pointer to the ticket? ----------
#
# People address the agent both ways:
#
#   "@Aetherion Allow drivers to upload a photo."   -> the comment IS the work
#   "@Aetherion add more description and create
#    some more sub tasks"                           -> "work from THIS ticket"
#
# The second is an instruction about the ticket, not a product requirement.
# Read as work it produces a Story called "Add more descrption", which is what
# happened on FL-27.

# Phrases that ask for work and name where the requirement lives. Each of
# these is self-sufficient: it states both the intent and the source.
_POINTER_PHRASES = re.compile(
    r"""(?:
          \b(?:add|write|put)\b[^.!?]*\bdescription\b
        | \b(?:create|add|make|generate)\b[^.!?]*\b(?:sub[-\s]?tasks?|stories|tickets?|epics?)\b
        | \bbreak\s+(?:this|it|the\s+\w+)\s+down\b
        | \brefer(?:ence|ring)?\s+(?:to\s+)?[A-Z][A-Z0-9]+-\d+\b
        | \b(?:as|per)\s+(?:described|above|below|mentioned)\b
        # "from the description", "based on the descrption" (BGV-3, typo and
        # all), "as per the ticket" — the work is whatever the ticket says.
        # Up to two words may sit between: "based on the give description".
        | \b(?:from|based\s+on|according\s+to|using|as\s+per|per)\s+(?:\w+\s+){0,2}?
           (?:description|descr\w*|ticket|story|details)\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# "this ticket" names a source but states no intent, so on its own it means
# nothing. It used to sit in the list above, which made
# "@Aetherion please explain me this ticket properly" indistinguishable from
# "@Aetherion please break this down into stories": the question was read as an
# instruction to decompose, and answering it created sub-tasks on a live ticket.
_BARE_TICKET_REFERENCE = re.compile(
    r"\b(?:this|the|current)\s+(?:ticket|issue|task|story|item)\b",
    re.IGNORECASE,
)

# ...so a bare reference only counts when the comment also asks for work.
# "explain", "summarise" and "describe" are deliberately absent: they ask for an
# answer, not for tickets.
_POINTER_WORK_VERBS = re.compile(
    r"\b(?:break|decompose|split|create|add|make|generate|write|put|expand|"
    r"detail|flesh|use|read|work|take|refer|reference|referring|update)\b",
    re.IGNORECASE,
)

# Used only to strip the instruction out before checking what is left.
_ANY_POINTER_TEXT = re.compile(
    f"(?:{_POINTER_PHRASES.pattern}|{_BARE_TICKET_REFERENCE.pattern})",
    re.IGNORECASE | re.VERBOSE,
)

# Words that mean real product work. A comment containing one of these is
# stating a requirement even if it also mentions "tickets".
_PRODUCT_WORDS = re.compile(
    r"\b(?:allow|let|enable|support|user|users|customer|customers|driver|drivers|"
    r"manager|managers|dispatcher|dispatchers|tenant|tenants|landlord|landlords|"
    r"admin|admins|so\s+that|should\s+be\s+able)\b",
    re.IGNORECASE,
)


# A comment that is nothing but a build verb — "@Aetherion build", "decompose
# this please". The router routes on these same verbs (catalog.py, BUILD), so
# the comment says only "break this ticket down". Without this, BGV-3's
# "@Aetherion build" was judged as the one-word requirement "build" and rejected
# as "not a work requirement", its description ignored (2026-09-24).
_BARE_BUILD_VERB = re.compile(
    r"""^(?:please\s+)?
        (?:build|break\s*down|breakdown|decompose|create\s+(?:the\s+)?tickets?)
        (?:\s+(?:this|it|this\s+(?:ticket|story|issue|task)))?
        (?:\s+please)?$""",
    re.IGNORECASE | re.VERBOSE,
)


def is_pointer_comment(text: str) -> bool:
    """True when a comment tells the agent to work from the ticket itself.

    Deliberately conservative: a comment that also names real product language
    ("allow drivers to…") is treated as a requirement even if it mentions
    tickets, because losing a genuine requirement is worse than reading one
    instruction the long way round.
    """
    body = (text or "").strip()
    if not body:
        return False
    bare = re.sub(r"[^\w\s]", " ", strip_trigger_mentions(body, trigger_keyword()))
    if _BARE_BUILD_VERB.match(re.sub(r"\s+", " ", bare).strip()):
        return True
    if not _POINTER_PHRASES.search(body):
        # Only a bare "this ticket" — an instruction to work on it must be
        # present too, or a question about the ticket reads as a request to
        # decompose it.
        if not (_BARE_TICKET_REFERENCE.search(body) and _POINTER_WORK_VERBS.search(body)):
            return False
    if _PRODUCT_WORDS.search(body):
        return False

    # Take the instruction out and see whether a requirement is still standing.
    # "Aetherion please break this down. Capture a photo of the drop-off …"
    # is an instruction AND a requirement — the requirement wins, or the work
    # being asked for is silently thrown away.
    from src.classification.validate import has_action_intent

    remainder = _ANY_POINTER_TEXT.sub(" ", body)
    remainder = re.sub(r"[^\w\s]", " ", remainder)
    remainder = re.sub(r"\s+", " ", remainder).strip()
    if has_action_intent(remainder) and len(remainder.split()) >= 4:
        return False

    return True


# A request to be told something, rather than to have something built. These
# used to fall through to the decomposer, which answered "explain this ticket"
# by creating sub-tasks on it.
_QUESTION_PHRASES = re.compile(
    r"""(?:
          \b(?:explain|summaris|summariz|describe|clarify|elaborate)\w*\b
        # "what" must open a question. Bare "what" also matched "what-if",
        # turning "Add a what-if calculator" into a question.
        | \bwhat(?:\s+(?:is|are|was|were|does|do|did|happens|kind|sort|exactly)|'s)\b
        | \bwhy\s+(?:is|are|was|were|do|does|did)\b
        | \bhow\s+(?:does|do|is|are|should)\b
        | \btell\s+me\b
        | \bwalk\s+me\s+through\b
        | \bgive\s+me\s+(?:a\s+)?(?:summary|overview|rundown)\b
        | \b(?:can|could)\s+you\s+(?:explain|summaris|summariz|describe)\w*\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def is_question_comment(text: str) -> bool:
    """True when the comment asks to be told something, not to have work created.

    Checked *before* the requirement path, so a question never reaches the
    decomposer. Product language wins, exactly as it does for pointer comments:
    "Explain how drivers log a break, and let dispatchers see it" states a
    requirement and must not be answered with prose instead of tickets.
    """
    from src.classification.validate import _is_small_talk

    body = (text or "").strip()
    if not body or not _QUESTION_PHRASES.search(body):
        return False
    if _PRODUCT_WORDS.search(body):
        return False
    # "What is the weather today?" is a question, but not one about the ticket.
    # This gate runs before validation, so anything it claims never reaches the
    # out-of-scope check — and the agent politely explained the ticket instead
    # of refusing.
    if _is_small_talk(strip_trigger_mentions(body, trigger_keyword())):
        return False
    return True


def describe_issue(source: SourceIssue) -> str:
    """Plain-English answer to "what is this ticket?", from the ticket itself.

    Deliberately says only what the ticket holds. Nothing here is generated
    from a model, so nothing can be invented — the cost is that this reads as a
    faithful summary rather than an interpretation.
    """
    key = source.key or "This issue"
    lines: list[str] = []

    kind = (source.issue_type or "issue").strip()
    status = (source.status or "").strip()
    opener = f"{key} is a {kind}"
    if status:
        opener += f", currently {status}"
    if source.reporter:
        opener += f", raised by {source.reporter}"
    lines.append(opener + ".")

    summary = strip_trigger_mentions(source.summary, trigger_keyword()).strip()
    if summary:
        lines.append(f'Its title is "{summary}".')

    body = strip_trigger_mentions(source.description, trigger_keyword()).strip()
    if body:
        lines.append("What it asks for, in its own words:")
        lines.append(body)
    else:
        lines.append("It has no description, so it does not say what is wanted.")

    readable = [p for p in source.confluence_pages if p.usable]
    if readable:
        titles = ", ".join(f'"{p.title or p.url}"' for p in readable)
        lines.append(f"It links {len(readable)} Confluence page(s): {titles}.")

    usable_files = [a for a in source.attachments if a.usable]
    if usable_files:
        names = ", ".join(a.filename for a in usable_files)
        lines.append(f"It has {len(usable_files)} readable attachment(s): {names}.")

    if source.linked_issues:
        links = ", ".join(f"{li.relationship} {li.key}" for li in source.linked_issues)
        lines.append(f"It is linked to: {links}.")

    if source.comments:
        lines.append(f"There are {len(source.comments)} comment(s) on it.")

    lines.append(
        "Nothing was created — this was a question, not a request for work. "
        "To have it broken down, comment asking for that, for example "
        '"break this down into stories".'
    )
    return "\n\n".join(lines)


def clean_requirement_text(text: str, keyword: str = "") -> str:
    """Everything that turns raw ticket text into a usable requirement.

    Removes, in order: sentences addressed to the agent, the trigger mention
    itself, any placement instruction, and any bare URL. Applied to **whichever
    field carries the requirement** — a description or, for a backlog
    quick-add, the summary.
    """
    cleaned = strip_agent_instructions((text or "").strip(), keyword or trigger_keyword())
    cleaned = strip_placement_instructions(cleaned)
    return strip_urls(cleaned).strip()


# "SMS is out of scope for this release, email only." A decision like this is
# posted as an ordinary comment, and comments are context — stripped before
# anything is decomposed. So the narrowing was read by every human on the
# ticket and by nothing in the agent, and the excluded work was built anyway.
_EXCLUSION_RE = re.compile(
    r"""(?:
          (?P<a>[^.!?\n]{2,80}?)\s+(?:is|are|will\s+be|has\s+been|have\s+been)\s+
            (?:out\s+of\s+scope|excluded|dropped|descoped|de-scoped|deferred|postponed)
        | (?:no|not)\s+(?P<b>[^.!?\n]{2,80}?)\s+(?:for\s+now|in\s+this\s+release|this\s+time)
        | (?:skip|drop|exclude|remove|leave\s+out)\s+(?P<c>[^.!?\n]{2,80}?)
            (?:\s+(?:for\s+now|from\s+this|in\s+this\s+release))
        | (?:only|just)\s+(?P<d>[^.!?\n]{2,60}?)\s+(?:for\s+now|in\s+this\s+release|this\s+time)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Words that carry no meaning on their own, so an exclusion made only of these
# would match every capability and empty the breakdown.
_EXCLUSION_STOPWORDS = frozenset(
    {"it", "this", "that", "them", "those", "these", "work", "thing", "things", "part", "the"}
)


def find_scope_exclusions(source: SourceIssue) -> list[str]:
    """Things a later comment said not to build.

    Only comments are read, and only ones the agent did not write: the ticket's
    own description states what *is* wanted, and the trigger comment is the
    request itself.
    """
    from src.jira.keywords import is_agent_comment

    found: list[str] = []
    for comment in source.comments:
        if comment.id and comment.id == source.trigger_comment_id:
            continue
        if is_agent_comment(comment.body):
            continue
        for match in _EXCLUSION_RE.finditer(comment.body or ""):
            phrase = next((g for g in match.groups() if g), "")
            phrase = re.sub(r"[^\w\s-]", " ", phrase).strip().lower()
            phrase = re.sub(r"\s+", " ", phrase)
            words = [w for w in phrase.split() if w not in _EXCLUSION_STOPWORDS]
            if words and phrase not in found:
                found.append(" ".join(words))
    return found


# Numbers people actually write in a requirement.
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "a": 1,
    "an": 1,
    "single": 1,
}

# "up to five saved cards" -> (5, "cards"). The head noun is the last word, so
# "five saved cards" and "5 cards" agree on what is being counted.
_QUANTITY_RE = re.compile(
    r"\b(?P<count>\d{1,4}|" + "|".join(_NUMBER_WORDS) + r")\s+(?P<window>(?:[a-z]+\s*){0,4})",
    re.IGNORECASE,
)

_QUANTITY_IGNORE = frozenset({"the", "that", "this", "each", "any", "all", "not", "are", "can"})

# Words that end the thing being counted. "one saved card per customer" counts
# cards, not customers, so the window has to stop at "per".
_QUANTITY_STOP = frozenset(
    {"per", "for", "of", "in", "on", "at", "with", "and", "or", "to", "from", "by", "each"}
)


def _quantities(text: str) -> dict[str, set[int]]:
    """Every "<number> <thing>" claim in ``text``, keyed by the thing."""
    found: dict[str, set[int]] = {}
    for match in _QUANTITY_RE.finditer(text or ""):
        raw = match.group("count").lower()
        value = _NUMBER_WORDS.get(raw, None)
        if value is None:
            try:
                value = int(raw)
            except ValueError:
                continue
        # The head noun, not the nearest word: "one saved card" is about cards,
        # and keying it on "saved" makes it disagree with "five cards" only by
        # accident.
        words: list[str] = []
        for word in match.group("window").split():
            lowered = word.lower()
            if lowered in _QUANTITY_STOP:
                break
            words.append(lowered.rstrip("s"))
        nouns = [w for w in words if len(w) >= 3 and w not in _QUANTITY_IGNORE]
        if not nouns:
            continue
        found.setdefault(nouns[-1], set()).add(value)
    return found


def find_unread_links(source: SourceIssue) -> list[str]:
    """Links in the request that contributed nothing.

    A link on another site is never fetched, and a Confluence page can be
    deleted or invisible. Either way the words behind it never arrive, and
    :func:`strip_urls` removes the URL — so "Build what this says: <link>"
    becomes "Build what this says", which reads like a requirement and is not
    one. Naming the ignored link is what stops that being silent.
    """
    request = f"{source.trigger_comment_body or ''}\n{source.description or ''}"
    read = {p.url for p in source.confluence_pages if p.usable}
    read |= {p.url.rstrip("/") for p in source.confluence_pages if p.usable}

    out: list[str] = []
    for raw in _URL_RE.findall(request):
        url = raw.rstrip(".,;:!?)]}>\"'")
        if url in read or url.rstrip("/") in read:
            continue
        if url not in out:
            out.append(url)
    return out


def request_is_only_a_pointer(source: SourceIssue) -> bool:
    """True when the request says nothing once its unusable links are removed.

    "Build what this spec says: <link>" is not a requirement when the link was
    never read. It passed validation because "build" is an action word, and was
    then matched against existing tickets on the four words that remained.
    """
    if not find_unread_links(source):
        return False
    stated = clean_requirement_text(source.trigger_comment_body, trigger_keyword())
    return len(stated.split()) <= 6


def _conflicting_pages(source: SourceIssue) -> tuple[list[str], set[str]]:
    """Contradictions between the ticket and its linked pages.

    Returns ``(messages, page_ids_that_contradict)``.

    A linked specification is usually newer than the ticket pointing at it, so
    the two disagreeing is normal and worth saying out loud. Nothing here
    decides which is right.
    """
    ticket = _quantities(strip_trigger_mentions(source.description, trigger_keyword()))
    if not ticket:
        return [], set()

    messages: list[str] = []
    clashing: set[str] = set()
    for page in source.confluence_pages:
        if not page.usable:
            continue
        where = page.title or page.url or "the linked page"
        for noun, page_values in _quantities(page.text).items():
            ticket_values = ticket.get(noun)
            if not ticket_values or ticket_values == page_values:
                continue
            if ticket_values & page_values:
                continue
            clashing.add(page.page_id or page.url)
            messages.append(
                f"{source.key or 'The ticket'} says "
                f"{'/'.join(str(v) for v in sorted(ticket_values))} {noun}(s); "
                f'"{where}" says {"/".join(str(v) for v in sorted(page_values))}. '
                f"The ticket was used, so nothing from that page went into these "
                f"tickets. Check which is right."
            )
    return messages, clashing


def find_conflicts(source: SourceIssue) -> list[str]:
    """The contradiction messages shown on the ticket."""
    return _conflicting_pages(source)[0]


def build_sections(source: SourceIssue) -> list[tuple[str, str]]:
    """Return ``(name, rendered_text)`` for every populated section."""
    sections: list[tuple[str, str]] = []
    keyword = trigger_keyword()

    # A request is made by commenting on a ticket, so the triggering comment is
    # the requirement. The ticket's own description says what the ticket is
    # about — useful background, but not what is being asked for now.
    if source.trigger_comment_body and is_pointer_comment(source.trigger_comment_body):
        # "add more description and create some more sub tasks" is an
        # instruction about this ticket, not a product requirement. The work
        # being asked for is whatever the ticket itself already describes.
        stated = clean_requirement_text(source.description, keyword)
        origin = (
            f"the description of {source.key or 'this ticket'}, as asked for in a "
            f"comment by {source.trigger_comment_author or 'a colleague'}"
        )
    elif source.trigger_comment_body:
        # Somebody asked for something. Whatever they wrote IS the request —
        # even if it is empty once the mention is removed. Falling back to the
        # ticket's description here would silently decompose the ticket when a
        # person commented only "@Aetherion" and forgot to say what they wanted.
        stated = clean_requirement_text(source.trigger_comment_body, keyword)
        origin = f"comment by {source.trigger_comment_author or 'a colleague'}"
    else:
        # No comment: a manual run, where the requirement was passed in directly
        # and arrives as the description.
        stated = clean_requirement_text(source.description, keyword)
        origin = "the request"

    readable_pages = [p for p in source.confluence_pages if p.usable]

    # "@Aetherion <link>" says nothing once the URL is stripped, but the person
    # did state a requirement — it is on the page they linked. Promote it to
    # the stated requirement rather than emitting the placeholder, which
    # otherwise reached Jira inside an Epic summary reading
    # "(no description provided) Driver Rest Breaks - Allow drivers to ...".
    promoted_page = None
    if not stated and readable_pages:
        promoted_page = readable_pages[0]
        stated = promoted_page.text.strip()
        origin = f'the linked Confluence page "{promoted_page.title or promoted_page.url}"'

    # The mention is stripped here too: left in, it reaches logs and subjects.
    display_summary = strip_trigger_mentions(source.summary, keyword)
    header = f"Stated requirement ({origin} on {source.key or 'the Jira issue'})"
    sections.append(("description", _section(header, stated or NO_REQUIREMENT_STATED)))

    usable = [a for a in source.attachments if a.usable]
    if usable:
        parts = [f"### Attachment: {a.filename}\n{a.text.strip()}" for a in usable]
        sections.append(
            (
                "attachments",
                _section(HEADING_ATTACHMENTS, "\n\n".join(parts)),
            )
        )

    # A page that contradicts the ticket is left out entirely. Merging it
    # produced sub-tasks reading "...one saved card per customer and Customers
    # may store up to five saved cards" — the contradiction baked into the work
    # item, while the reply claimed the ticket had been used.
    _, clashing = _conflicting_pages(source)

    # A page promoted above is already the stated requirement; repeating it here
    # would feed the same text to the decomposer twice.
    remaining_pages = [
        p for p in readable_pages if p is not promoted_page and (p.page_id or p.url) not in clashing
    ]
    if remaining_pages:
        parts = []
        for p in remaining_pages:
            block = f"### Confluence page: {p.title or p.url}\n{p.text.strip()}"
            # A page often says "see the attached sheet" and nothing else. The
            # file is the requirement, so it goes in under the page it hangs off.
            for att in p.attachments:
                if att.text:
                    block += f"\n\n#### Attached to this page: {att.filename}\n{att.text.strip()}"
            parts.append(block)
        sections.append(("confluence", _section(HEADING_CONFLUENCE, "\n\n".join(parts))))

    exclusions = find_scope_exclusions(source)
    if exclusions:
        sections.append(
            (
                "exclusions",
                _section(HEADING_EXCLUSIONS, "\n".join(f"- {item}" for item in exclusions)),
            )
        )

    ticket_desc = strip_trigger_mentions(source.description.strip(), keyword)
    if ticket_desc and ticket_desc != stated:
        sections.append(("ticket", _section(HEADING_TICKET, f"{display_summary}\n{ticket_desc}")))

    if source.comments:
        parts = [
            f"[{c.created or 'undated'}] {c.author or 'unknown'}: {c.body}" for c in source.comments
        ]
        sections.append(
            (
                "comments",
                _section(
                    HEADING_COMMENTS,
                    "\n".join(parts),
                ),
            )
        )

    if source.linked_issues:
        parts = [
            f"- {link.key} ({link.relationship}): {link.summary}"
            f"{f' [{link.issue_type}, {link.status}]' if link.issue_type else ''}"
            for link in source.linked_issues
        ]
        sections.append(
            (
                "links",
                _section(HEADING_LINKS, "\n".join(parts)),
            )
        )

    if source.history:
        parts = [f"- {entry.as_line()}" for entry in source.history]
        sections.append(("history", _section(HEADING_HISTORY, "\n".join(parts))))

    if source.worklogs:
        parts = [
            f"- {w.started or 'undated'} {w.author or 'unknown'} logged {w.time_spent}"
            f"{f': {w.comment}' if w.comment else ''}"
            for w in source.worklogs
        ]
        sections.append(("worklogs", _section(HEADING_WORKLOGS, "\n".join(parts))))

    return sections


def assemble_requirement(source: SourceIssue) -> tuple[str, list[str]]:
    """Build the requirement text plus notes about anything dropped.

    Returns ``(text, notes)``. ``notes`` records every section omitted to fit
    the budget, so a thin breakdown can be traced back to a trimmed input
    rather than looking like the model ignored something.
    """
    sections = build_sections(source)
    budget = max_context_chars()
    notes: list[str] = []

    def rendered(items: list[tuple[str, str]]) -> str:
        return "\n\n".join(text for _, text in items)

    kept = list(sections)
    for name in _DROP_ORDER:
        if len(rendered(kept)) <= budget:
            break
        before = len(kept)
        kept = [item for item in kept if item[0] != name]
        if len(kept) < before:
            notes.append(f"Section '{name}' was omitted to stay within the context budget.")

    text = rendered(kept)
    if len(text) > budget:
        # Only the description is left and it is still too long. Truncating here
        # is the last resort, and it is said out loud.
        text = text[:budget]
        notes.append(
            f"The description was truncated to {budget} characters to fit the context budget."
        )

    logger.info(
        f"Assembled requirement: {len(text)} chars from sections " f"{[name for name, _ in kept]}"
    )
    return text, notes
