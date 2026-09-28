"""Deciding what kind of comment this is, before anything else happens.

Three buckets, and they get very different treatment:

* **A — not a requirement.** "who is the pm of India?", "I want to go home
  early", "hi", "thanks". One short line back, nothing created, and **never an
  email**. These are the comments that used to be answered with a list of
  clarifying questions and an alert, turning idle chatter into an interruption.
* **B — a requirement, but too thin to build.** "add export to the reports
  page" names a real change and leaves out everything needed to size it. Two or
  three specific questions, nothing created, and no email unless asked for.
* **C — a valid requirement.** Size it, check for duplicates, build it.

The split between A and B is the one that matters most: A is about *whether
there is work here at all*, and B is about *whether there is enough detail*.
Answering "which project is this for?" to someone asking where the PM is gets
that backwards.

Nothing here decides B versus C perfectly — that is a judgement a language
model makes better than a rule. The rules below are deliberately cautious in
one direction: when in doubt between "thin" and "ready", ask rather than build,
because an unnecessary question costs a comment and a wrong Epic costs a
clean-up.
"""

from __future__ import annotations

import re

from common_lib.utils.logger import setup_logger

from src.classification.validate import (
    _MIN_WORDS_FOR_CLARIFICATION,
    _WORD_RE,
    ProjectContext,
    has_action_intent,
    validate_input,
)
from src.models.schemas import RequestBucket, ResultStatus

logger = setup_logger(__name__)


# Someone the work is for. A named actor is the strongest single sign that a
# short sentence is a real requirement rather than a passing thought: "allow
# drivers to log a break" versus "add export".
_ACTOR_RE = re.compile(
    r"\b(?:user|users|customer|customers|driver|drivers|manager|managers|admin|admins"
    r"|dispatcher|dispatchers|agent|agents|operator|operators|staff|team|teams"
    r"|tenant|tenants|landlord|landlords|owner|owners|supervisor|supervisors"
    r"|member|members|client|clients|employee|employees|finance|support"
    r"|someone|anyone|everyone|people)\b",
    re.IGNORECASE,
)

# Detail that shows the writer thought about the edges: a limit, a condition, a
# format, a trigger. Any of these makes a short sentence buildable.
_DETAIL_RE = re.compile(
    r"\b(?:\d+"
    r"|when|if|unless|until|before|after|per|each|every"
    r"|must|only|at\s+least|at\s+most|no\s+more\s+than"
    r"|csv|pdf|xlsx|excel|json|email|sms|so\s+that)\b",
    re.IGNORECASE,
)

# Enough words that the writer has clearly described something.
_SUBSTANTIAL_WORDS = 12


# A question about the world, not a request for work. "who is the pm of India?"
# has six real words and would otherwise look substantial enough to be worth a
# second opinion — it is not, and sending it to the model only to be told so
# costs a gateway call on every piece of idle chatter.
_INTERROGATIVE_RE = re.compile(
    r"^\s*(?:who|whose|whom|where|when|which|what|why|how|is|are|was|were|do|does|"
    r"did|can|could|will|would|should|shall|has|have|had)\b",
    re.IGNORECASE,
)

# Someone talking about themselves rather than about the product. "I want to go
# home early" and "I am feeling hungry and I want to eat biryani" both reach
# here: their only recognised word is a wish word, which has_action_intent
# already discounts.
_FIRST_PERSON_WISH_RE = re.compile(
    r"^\s*(?:i|we)\b[^.?!]{0,60}?\b(?:want|need|wish|would\s+like|feel|am|'m)\b",
    re.IGNORECASE,
)

# Language that means the sentence is about the product, whoever is speaking.
# Mirrors ``ingest._PRODUCT_WORDS``; kept local so this module does not import
# the ingest layer (and with it the environment) just for one pattern.
_PRODUCT_LANGUAGE_RE = re.compile(
    r"\b(?:user|users|customer|customers|driver|drivers|manager|managers|admin|"
    r"admins|dispatcher|dispatchers|tenant|tenants|landlord|landlords|operator|"
    r"operators|staff|team|client|clients|employee|employees|ticket|tickets|"
    r"page|pages|screen|screens|report|reports|field|fields|form|forms|api|"
    r"endpoint|database|record|records|button|dashboard|export|import|login|"
    r"so\s+that|should\s+be\s+able)\b",
    re.IGNORECASE,
)


def _is_substantial(request: str) -> bool:
    """True when the text is real language a person could be asked about.

    Gibberish, small talk and one-liners have already been removed by the
    deterministic gate before this is reached. What is left to rule out is
    chatter that happens to be long enough: a question about the world, and
    somebody talking about themselves. Both carry product language when they
    are genuinely about the product, so that is what decides it.
    """
    if len(_WORD_RE.findall(request)) < _MIN_WORDS_FOR_CLARIFICATION:
        return False
    if _PRODUCT_LANGUAGE_RE.search(request):
        return True
    if _INTERROGATIVE_RE.search(request):
        return False
    if _FIRST_PERSON_WISH_RE.search(request):
        return False
    return True


def classify_request(
    text: str,
    context: ProjectContext | None = None,
) -> tuple[RequestBucket, list[str]]:
    """Sort a comment into bucket A, B or C.

    Returns ``(bucket, questions)``. ``questions`` is empty for A and C: bucket
    A is never asked anything, because there was no requirement to clarify.
    """
    request = (text or "").strip()

    # --- A: nothing to build -------------------------------------------
    verdict = validate_input(request, context)
    if verdict.status in (ResultStatus.VALIDATION_ERROR, ResultStatus.OUT_OF_SCOPE):
        return RequestBucket.NOT_A_REQUIREMENT, []

    # The deterministic gate accepts anything carrying a work verb. "who is the
    # pm of India?" has none, and neither does "I want to go home early" — both
    # used to be answered with clarifying questions about the project.
    #
    # But _ACTION_WORDS is a hand-maintained list and it has been wrong in
    # production three times already, each time rejecting a real requirement
    # ("Log a rest break.", "alert the depot manager when a fault is reported").
    # Verbs it still does not know include paginate, throttle, encrypt, expire,
    # toggle, redact and aggregate. Rejecting on the list alone means every
    # future gap is a real requirement told it is invalid, with no email, no
    # question and no record.
    #
    # So an unrecognised verb only rejects outright when the text is too thin
    # to be a requirement at all. Readable, substantial text goes to the model.
    if not has_action_intent(request):
        if _is_substantial(request):
            return RequestBucket.UNCERTAIN, _questions_for(request)
        return RequestBucket.NOT_A_REQUIREMENT, []

    # --- C: enough to work from ----------------------------------------
    from src.classification.decompose import split_capabilities

    words = request.split()
    capabilities = split_capabilities(request)

    if (
        len(capabilities) > 1
        or len(words) >= _SUBSTANTIAL_WORDS
        or (_ACTOR_RE.search(request) and _DETAIL_RE.search(request))
        or (_ACTOR_RE.search(request) and len(words) >= 6)
    ):
        return RequestBucket.VALID, []

    # --- B: a real change, described too thinly -------------------------
    return RequestBucket.INCOMPLETE, _questions_for(request)


def _questions_for(request: str) -> list[str]:
    """The two or three things actually blocking a breakdown.

    Only what is missing is asked. A question about something the requirement
    already answers reads as though nobody looked at it.
    """
    questions: list[str] = []

    if not _ACTOR_RE.search(request):
        questions.append("Who is this for — which role or type of user?")
    if not _DETAIL_RE.search(request):
        questions.append(
            "What are the rules or limits? For example a format, a maximum, "
            "or when it should happen."
        )
    questions.append("How will you know it is done — what does success look like?")

    return questions[:3]


__all__ = ["RequestBucket", "classify_request"]
