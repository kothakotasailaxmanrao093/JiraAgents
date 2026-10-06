"""Jira Cloud REST v3 integration for the work-breakdown agent.

Follows the conventions already established in the sibling ``asurint`` project:
credentials from the environment, one pooled ``httpx.AsyncClient`` per call,
secrets only ever logged through ``_mask``, JQL string values escaped, and
failures returned as data rather than raised across the activity boundary.

Two behaviours are specific to this agent:

* **Preflight.** The Jira project and the issue types the breakdown needs are
  verified *before* the first write, so a missing Sub-task type fails with
  nothing created rather than half a hierarchy.
* **Idempotency.** Every hierarchy carries a deterministic label derived from
  the requirement text and target project. A retry finds that label and returns
  the existing issues instead of creating duplicates.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

from common_lib.utils.logger import setup_logger

from src.config.settings import env_bool, env_float
from src.jira.client import browse_url, issue_type_names
from src.jira.trigger import awaiting_label, processed_label
from src.models.schemas import (
    DuplicateMatch,
    ExistingIssue,
    OverlapReport,
)

logger = setup_logger(__name__)


# --------------------------------------------------------------------------
# Overlap detection against tickets that already exist
# --------------------------------------------------------------------------

_STOPWORDS = frozenset(
    """a an the and or of to for in on at by with as is are be being been this that
    these those it its from into over under user users allow allows enable enables
    support supports let lets add adds create creates make makes system should must
    can will new
    """.split()
    # Pronouns carry no capability meaning, and leaving them in is what made
    # "Let them view their payment history" and "Let tenants view their payment
    # history" score 0.667 instead of 0.750 — the only differing token was the
    # pronoun. Stripping them separates a genuine reword from a sibling
    # capability without touching the metric or the threshold.
    + """them their they theirs it its he she him her his hers we our ours
    you your yours me my mine us""".split()
)


@lru_cache(maxsize=65_536)
def _tokens(text: str) -> frozenset[str]:
    """The meaningful words of a text — worked out once per distinct text.

    Every rule compares a proposed title with a ticket's summary and its
    description, so the same ticket texts are split into words again for every
    title. Remembering the answer (tokenise once) makes that free; 65,536
    texts is ~20 MB, a whole 18,000-ticket project with room to spare.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return frozenset(w for w in words if len(w) > 2 and w not in _STOPWORDS)


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of meaningful words. 1.0 identical, 0.0 unrelated.

    Deliberately simple and explainable: it catches rewordings of the same
    capability without pretending to be semantic search.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Deliberately high. At 0.6, sibling capabilities collide: "Send email
# notifications to customers" and "Send SMS notifications to customers" score
# 0.6 against each other and would be reported as duplicates, which is worse
# than missing a reword. Bag-of-words cannot tell a paraphrase from a sibling,
# so this stays conservative and the model handles genuine paraphrase when the
# gateway is available.
_DEFAULT_DUPLICATE_THRESHOLD = 0.75


def duplicate_threshold() -> float:
    value = env_float("LTW_DUPLICATE_THRESHOLD", _DEFAULT_DUPLICATE_THRESHOLD)
    return min(max(value, 0.0), 1.0)


def is_request_ticket(issue: ExistingIssue) -> bool:
    """True when a ticket is somebody *asking* for work, not the work itself.

    A trigger ticket carries the requirement in its own description and gets a
    marker label once the agent has read it. Comparing proposed Stories against
    those descriptions matches every time — most obviously the trigger ticket
    matching the very Stories derived from it ("'Record rest breaks' is covered
    by TT2-164", where TT2-164 *was* the request).
    """
    markers = {processed_label(), awaiting_label()}
    return any(str(label) in markers for label in issue.labels)


# Jira's own status grouping. The status *name* is whatever the team invented
# — "Won't Do", "Abandoned", "Shipped" — so only the category is dependable.
_DONE_CATEGORY = "done"


def is_closed(issue: ExistingIssue) -> bool:
    """True when Jira considers this ticket finished or abandoned."""
    return issue.status_category.strip().lower() == _DONE_CATEGORY


def compare_closed_work() -> bool:
    """Whether finished work still counts as a duplicate. Off by default.

    Work closed as "Won't Do" is a decision *not* to build something, and work
    closed as "Done" is already delivered. Neither is a reason to refuse a new
    request, but both used to block one: nothing filtered by status at any
    layer, so a request matching an abandoned ticket was answered with "this
    already exists" and nothing was created.

    Set ``LTW_MATCH_CLOSED_WORK=true`` to go back to comparing everything.
    """
    return env_bool("LTW_MATCH_CLOSED_WORK")


def comparable_issues(
    existing: list[ExistingIssue],
    exclude_keys: set[str] | None = None,
) -> list[ExistingIssue]:
    """Keep only tickets worth comparing a proposed Story title against.

    Four kinds are dropped:

    * **Sub-tasks** — their summaries carry long fixed prefixes ("Define the
      rules for: ..."), which drags every score down.
    * **Request tickets** — see :func:`is_request_ticket`; they restate the
      requirement rather than implement it.
    * **Anything named in** ``exclude_keys`` — the trigger issue itself.
    * **Closed work** — see :func:`compare_closed_work`.
    """
    subtask_name = issue_type_names()["subtask"].strip().lower()
    skip_types = {"sub-task", "subtask", subtask_name}
    skip_keys = {k.strip().upper() for k in (exclude_keys or set())}
    match_closed = compare_closed_work()
    return [
        issue
        for issue in existing
        if issue.issue_type.strip().lower() not in skip_types
        and issue.key.strip().upper() not in skip_keys
        and not is_request_ticket(issue)
        and (match_closed or not is_closed(issue))
    ]


def best_overlap(
    proposed_titles: list[str],
    existing: list[ExistingIssue],
    exclude_keys: set[str] | None = None,
) -> DuplicateMatch | None:
    """The single closest existing ticket, regardless of the threshold.

    Reported even when nothing crosses the line, because an empty
    ``duplicate_matches`` gives no way to tell "nothing similar exists" from
    "the threshold is slightly too high".
    """
    best: DuplicateMatch | None = None
    for title in proposed_titles:
        for issue in comparable_issues(existing, exclude_keys):
            score = similarity(title, issue.summary)
            if best is None or score > best.score:
                best = DuplicateMatch(
                    proposed_title=title,
                    existing_key=issue.key,
                    existing_summary=issue.summary,
                    score=round(score, 3),
                )
    return best


def containment(needle: str, haystack: str) -> float:
    """How much of ``needle`` appears in ``haystack``. 1.0 = every word of it.

    Used against an existing ticket's *description*, where Jaccard is useless:
    a long description shares few words with a short title even when it plainly
    covers it. Containment asks the question that actually matters — "is this
    proposal already described in there?"
    """
    want, have = _tokens(needle), _tokens(haystack)
    if not want or not have:
        return 0.0
    return len(want & have) / len(want)


def description_containment(title: str, description: str) -> float:
    """Containment of a title in a description — per sentence for short titles.

    A two-word title is too easy to find scattered across a long description:
    "Officer ID Verification" is {officer, verification}, one in "Officer can
    mark each address…" and one in "…with verification status" — 1.00 against
    a ticket about address history, which blocked a build (BGV-57,
    2026-09-24). For such titles the words must share one sentence, as
    "password reset" does in "…can reset a forgotten password".
    """
    if len(_tokens(title)) >= 3:
        return containment(title, description)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", description)
    return max((containment(title, part) for part in sentences if part.strip()), default=0.0)


# Containment is a weaker signal than a title match, so it needs a higher bar
# before it blocks work. At 0.85 nearly every meaningful word of the proposed
# title must already appear in the existing description.
_DEFAULT_DESCRIPTION_THRESHOLD = 0.85


def description_threshold() -> float:
    raw = os.environ.get("LTW_DESCRIPTION_MATCH_THRESHOLD", "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_DESCRIPTION_THRESHOLD
    except ValueError:
        value = _DEFAULT_DESCRIPTION_THRESHOLD
    return min(max(value, 0.0), 1.0)


# Matching proposed *titles* only works while the generator words things the
# same way twice, and it does not: the model and the deterministic fallback
# produce different vocabularies for one requirement, and two near-identical
# Stories were created in FL because both title routes fell just short.
#
# The requirement text does not drift. It is what the person typed, byte for
# byte, on every re-post and every retry, so it is the one stable thing to
# compare. Measured against the real project: the genuine duplicate scored
# 0.833 and the nearest unrelated ticket 0.333, so 0.80 separates them with
# room to spare.
_DEFAULT_REQUIREMENT_THRESHOLD = 0.80


def requirement_threshold() -> float:
    raw = os.environ.get("LTW_REQUIREMENT_MATCH_THRESHOLD", "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_REQUIREMENT_THRESHOLD
    except ValueError:
        value = _DEFAULT_REQUIREMENT_THRESHOLD
    return min(max(value, 0.0), 1.0)


def find_requirement_match(
    requirement: str,
    existing: list[ExistingIssue],
    threshold: float | None = None,
    base_url: str = "",
    exclude_keys: set[str] | None = None,
) -> DuplicateMatch | None:
    """The existing ticket that already covers this requirement, if any.

    Compares the requirement against each ticket's summary *and* description
    together, because the wording a person used may land in either.
    """
    text = (requirement or "").strip()
    if not text:
        return None

    limit = requirement_threshold() if threshold is None else threshold
    best: DuplicateMatch | None = None

    for issue in comparable_issues(existing, exclude_keys):
        blob = f"{issue.summary}\n{issue.description or ''}"
        score = containment(text, blob)
        if score >= limit and (best is None or score > best.score):
            best = DuplicateMatch(
                proposed_title=text,
                existing_key=issue.key,
                existing_summary=issue.summary,
                score=round(score, 3),
                matched_on="requirement",
                existing_status=issue.status,
                existing_type=issue.issue_type,
                existing_url=issue.url or browse_url(base_url, issue.key),
            )
    return best


def _best_match_for(
    title: str,
    candidates: list[ExistingIssue],
    summary_limit: float,
    description_limit: float,
    base_url: str = "",
) -> DuplicateMatch | None:
    """The strongest collision for one proposed title, or None."""
    best: DuplicateMatch | None = None

    for issue in candidates:
        # 1. Same wording as an existing title.
        score = similarity(title, issue.summary)
        matched_on = "summary"

        # 2. Different wording, but the existing description already covers it.
        # Never an Epic's: it summarises the whole feature, so nearly any Story
        # in that area is "contained" in it — "Generate Monthly Invoices"
        # scored 1.00 against Epic BGV-26 while the closer Story was missed
        # (BGV-40, BGV-41, 2026-09-25). Epics still match on their title.
        is_epic = (issue.issue_type or "").strip().lower() == "epic"
        if score < summary_limit and issue.description and not is_epic:
            contained = description_containment(title, issue.description)
            if contained >= description_limit:
                score, matched_on = contained, "description"

        limit = summary_limit if matched_on == "summary" else description_limit
        if score >= limit and (best is None or score > best.score):
            best = DuplicateMatch(
                proposed_title=title,
                existing_key=issue.key,
                existing_summary=issue.summary,
                score=round(score, 3),
                matched_on=matched_on,
                existing_status=issue.status,
                existing_type=issue.issue_type,
                existing_url=issue.url or browse_url(base_url, issue.key),
            )
    return best


# --------------------------------------------------------------------------
# Near misses — the band a bag-of-words score cannot decide
# --------------------------------------------------------------------------

# Below this, two titles share so little wording that asking a model about them
# is noise. Between this and the threshold is the band where Jaccard is simply
# the wrong instrument: "Record a rest break" and "Allow drivers to log a rest
# break" are the same work and score 0.40, while "Send email notifications" and
# "Send SMS notifications" are different work and score 0.60. No threshold
# separates those two pairs. A model can.
#
# 0.30 rather than something higher because titles are normalised before they
# are compared — the generator turns "Let drivers record a rest break" into
# "Record a rest break", dropping the actor that the existing ticket still
# carries. Two or three shared content words is as much agreement as a genuine
# reword produces. Unrelated work still scores 0.0 and is never sent.
_DEFAULT_ADJUDICATION_FLOOR = 0.30

# Bound the prompt. Pairs are already one-per-proposed-title, so this only
# bites on a very large breakdown, and the strongest candidates go first.
MAX_ADJUDICATED_PAIRS = 10


def adjudication_floor() -> float:
    value = env_float("LTW_DUPLICATE_ADJUDICATE_FLOOR", _DEFAULT_ADJUDICATION_FLOOR)
    return min(max(value, 0.0), 1.0)


def adjudication_enabled() -> bool:
    """Semantic second opinion on near misses. On by default."""
    return env_bool("LTW_DUPLICATE_ADJUDICATE", True)


def find_near_misses(
    proposed_titles: list[str],
    existing: list[ExistingIssue],
    threshold: float | None = None,
    base_url: str = "",
    exclude_keys: set[str] | None = None,
) -> list[DuplicateMatch]:
    """Pairs that scored close but did not cross the line.

    Deliberately separate from :func:`find_overlaps`: these are candidates for
    a second opinion, never matches in their own right. If nothing adjudicates
    them they are discarded, which is exactly the behaviour before this existed.
    """
    limit = duplicate_threshold() if threshold is None else threshold
    floor = adjudication_floor()
    if floor >= limit:
        return []

    candidates = comparable_issues(existing, exclude_keys)
    near: list[DuplicateMatch] = []
    for title in proposed_titles:
        best: DuplicateMatch | None = None
        for issue in candidates:
            score = similarity(title, issue.summary)
            if not (floor <= score < limit):
                continue
            if best is None or score > best.score:
                best = DuplicateMatch(
                    proposed_title=title,
                    existing_key=issue.key,
                    existing_summary=issue.summary,
                    score=round(score, 3),
                    matched_on="near_miss",
                    existing_status=issue.status,
                    existing_type=issue.issue_type,
                    existing_url=issue.url or browse_url(base_url, issue.key),
                )
        if best:
            near.append(best)
    near.sort(key=lambda m: m.score, reverse=True)
    return near[:MAX_ADJUDICATED_PAIRS]


def find_overlaps(
    proposed_titles: list[str],
    existing: list[ExistingIssue],
    threshold: float | None = None,
    base_url: str = "",
    exclude_keys: set[str] | None = None,
) -> list[DuplicateMatch]:
    """Match proposed work against tickets already in the project.

    Compares each proposed title against every existing **summary** and, when
    that does not fire, against every existing **description** — which is how
    work whose title is worded differently is still recognised.
    """
    summary_limit = duplicate_threshold() if threshold is None else threshold
    description_limit = description_threshold()
    candidates = comparable_issues(existing, exclude_keys)

    matches: list[DuplicateMatch] = []
    for title in proposed_titles:
        best = _best_match_for(title, candidates, summary_limit, description_limit, base_url)
        if best:
            matches.append(best)
    return matches


def build_overlap_report(
    proposed_titles: list[str],
    existing: list[ExistingIssue],
    threshold: float | None = None,
    base_url: str = "",
    exclude_keys: set[str] | None = None,
    requirement: str = "",
) -> OverlapReport:
    """Split a proposal into what already exists and what does not.

    This is what lets the agent tell "all of this exists" apart from "half of
    this exists" — two situations that need different answers from a human.
    """
    matches = find_overlaps(proposed_titles, existing, threshold, base_url, exclude_keys)

    # Title matching depends on the generator wording things the same way twice.
    # When it has not, fall back to the requirement itself — if an existing
    # ticket already covers what was asked for, none of the proposal is new,
    # however differently this run happened to name it.
    if not matches and requirement:
        covering = find_requirement_match(requirement, existing, None, base_url, exclude_keys)
        if covering:
            matches = [covering.model_copy(update={"proposed_title": t}) for t in proposed_titles]

    covered = {match.proposed_title for match in matches}
    return OverlapReport(
        matches=matches,
        covered_titles=[t for t in proposed_titles if t in covered],
        remaining_titles=[t for t in proposed_titles if t not in covered],
    )
