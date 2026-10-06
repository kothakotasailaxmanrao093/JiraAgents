"""Tool activities for the work-breakdown agent.

Three activities, in pipeline order:

1. ``validate_requirement`` — deterministic gate plus LLM triage. Never writes.
2. ``generate_work_breakdown`` — classification and decomposition, schema-validated.
3. ``create_jira_issues`` — the only activity that writes to Jira, and only for
   a breakdown that has already passed validation.

Following the convention in the sibling ``asurint`` project, tools return plain
dicts carrying an ``error`` string rather than raising: a business failure is a
result, not something Temporal should retry.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import httpx
from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from pydantic import ValidationError

from src.classification.decompose import (
    DecompositionError,
    build_breakdown,
    explain_issue,
    llm_related,
    llm_same_work,
    requirement_lines,
    strip_section_headings,
    triage,
)
from src.classification.request import RequestBucket, classify_request
from src.classification.validate import ProjectContext, normalise, validate_input
from src.config.settings import env_bool
from src.confluence import client as confluence_mod
from src.context import attachments as attachments_mod
from src.context import uploads as uploads_mod
from src.context.ingest import (
    assemble_requirement,
    describe_issue,
    detect_placement,
    find_conflicts,
    find_unread_links,
    has_url,
    is_pointer_comment,
    is_question_comment,
    request_is_only_a_pointer,
    requirement_core,
    strip_trigger_mentions,
)
from src.document import breakdown_pdf as pdf
from src.jira import api as jira
from src.jira import root as root_mod
from src.jira import ticket_index
from src.models.schemas import (
    AttachmentText,
    CommentInfo,
    ConfluencePage,
    DuplicateMatch,
    ExistingIssue,
    JiraContext,
    JiraIssueRef,
    JiraResult,
    NotificationKind,
    OverlapReport,
    ResultStatus,
    RootChange,
    SourceIssue,
    SprintPlacement,
    WorkBreakdown,
    allow_source_files_from,
)
from src.notifications import email as notifier
from src.observability.timing import stage

logger = setup_logger(__name__)


def _context_from(payload: dict[str, Any] | None) -> ProjectContext:
    """Build project context, preferring what was actually read from Jira.

    ``inspect_jira_context`` supplies the project and its existing tickets. Only
    when that is absent (a bare tool call, or a unit test) does this fall back
    to environment variables.
    """
    data = payload or {}
    project = data.get("project")
    if project:
        # The ticket being worked on is not "existing work" to compare against.
        # Left in, the model asked BGV-3 whether it "differs from BGV-3"
        # (F36 note, seen again 2026-09-24). The overlap check already excludes
        # it via trigger_key; the model's context has to as well.
        own = normalise(data.get("trigger_key")).upper()
        existing = [
            i
            for i in data.get("existing_issues") or []
            if not own or str(i.get("key") or "").upper() != own
        ]
        return ProjectContext.from_jira(
            project,
            existing,
            name_override=normalise(data.get("project_name")),
        )
    overrides = {
        k: normalise(v)
        for k, v in data.items()
        if v is not None and isinstance(v, str | int | float | bool)
    }
    return ProjectContext.from_env(overrides)


def _existing_issues(payload: dict[str, Any] | None) -> list[ExistingIssue]:
    """Rehydrate the existing-ticket list handed over by the context tool."""
    out: list[ExistingIssue] = []
    for raw in (payload or {}).get("existing_issues") or []:
        try:
            out.append(ExistingIssue.model_validate(raw))
        except ValidationError:
            continue
    return out


def _context_failure(
    reason: str,
    notes: list[str] | None = None,
    needs_clarification: bool = False,
) -> dict[str, Any]:
    """A Jira-side problem found before any decomposition. Nothing was written.

    ``needs_clarification`` marks the failures the requester can answer — such
    as an ambiguous sprint — so the agent asks instead of reporting a fault.
    """
    logger.error(f"Jira context check failed: {reason}")
    return JiraContext(
        ok=False,
        error=reason,
        notes=notes or [],
        needs_clarification=needs_clarification,
    ).model_dump(mode="json")


# Listed requirement lines that make a request plainly work, so the model's
# triage is not asked. Three: a one- or two-line request is exactly where
# "is this a requirement at all?" is still a real question.
CLEAR_REQUIREMENT_LINES = 3

# How much the ticket asked on and the request share, as word containment,
# before the words alone decide relatedness (see _is_related).
RELATED_BY_WORDS = 0.5
UNRELATED_BY_WORDS = 0.2


async def _nothing() -> None:
    """A read that was not needed this run, for a slot in a gather."""
    return None


@tool()
async def inspect_jira_context(
    project_key: str | None = None,
    existing_epic_key: str | None = None,
    placement: str | None = None,
) -> dict[str, Any]:
    """Read and validate everything on the Jira side before anything is decided.

    Runs first, so an unknown project, a bad epic key, or a missing active
    sprint stops the run before a work breakdown is even generated — let alone
    written. Also returns the project's own description and its recent tickets,
    so relevance and overlap are judged against Jira reality rather than
    against whatever the user typed into a form.
    """
    with stage("inspect_jira_context", project=normalise(project_key).upper() or "-"):
        return await _inspect_jira_context(project_key, existing_epic_key, placement)


async def _inspect_jira_context(
    project_key: str | None = None,
    existing_epic_key: str | None = None,
    placement: str | None = None,
) -> dict[str, Any]:
    """The body of :func:`inspect_jira_context`, timed by it."""
    logger.info("Inside the inspect_jira_context tool")

    want = (
        SprintPlacement.CURRENT_SPRINT
        if (
            normalise(placement).lower().replace("-", "_")
            in {"current_sprint", "sprint", "current"}
        )
        else SprintPlacement.BACKLOG
    )

    base_url, email, token = jira.jira_creds()
    if not (base_url and email and token):
        return _context_failure(
            "Missing JIRA_BASE_URL, JIRA_EMAIL, or JIRA_API_TOKEN. " "No Jira issues were created."
        )

    key = jira.target_project_key(normalise(project_key))
    if not key:
        return _context_failure(
            "No Jira project key provided. Supply the Jira Project Key field "
            "(for example 'KS') or set JIRA_PROJECT_KEY. No Jira issues were created."
        )

    allowed = jira.allowed_project_keys()
    if allowed and key not in allowed:
        return _context_failure(
            f"Project '{key}' is not on this deployment's allowed list "
            f"({', '.join(allowed)}). No Jira issues were created. Ask an "
            f"administrator to add it to LTW_ALLOWED_PROJECT_KEYS."
        )

    notes: list[str] = []
    epic_key = normalise(existing_epic_key)

    try:
        async with jira.jira_client(base_url, email, token) as client:
            # All five reads are independent, so they run together; their
            # results are then checked in the same order as before, so the
            # first problem reported is still the same one.
            (
                project_read,
                types_read,
                existing_read,
                epic_read,
                sprint_read,
            ) = await asyncio.gather(
                jira.fetch_project(client, key),
                jira.available_issue_types(client, key),
                jira.fetch_existing_issues(client, key, base_url=base_url),
                jira.fetch_epic(client, epic_key, key) if epic_key else _nothing(),
                (
                    jira.active_sprint(client, key)
                    if want is SprintPlacement.CURRENT_SPRINT
                    else _nothing()
                ),
                return_exceptions=True,
            )

            # --- the project itself ---------------------------------------
            if isinstance(project_read, httpx.HTTPStatusError):
                exc = project_read
                if exc.response.status_code in (401, 403):
                    return _context_failure(
                        f"Jira authentication failed or access is denied for "
                        f"project '{key}': {jira._http_error_text(exc)}"
                    )
                if exc.response.status_code == 404:
                    # Jira answers 404 for both "no such project" and "bad
                    # credentials". Ask /myself which one it actually is,
                    # otherwise an expired token reads as a missing project.
                    if not await jira.is_authenticated(client):
                        return _context_failure(
                            "Jira authentication failed. Check JIRA_EMAIL and "
                            "JIRA_API_TOKEN — the credentials were rejected, so "
                            f"project '{key}' could not be read."
                        )
                    return _context_failure(
                        f"Jira project '{key}' was not found or is not visible to "
                        f"this account. Check the project key (short and "
                        f"uppercase, e.g. 'TT') and this account's Browse "
                        f"Projects permission."
                    )
                return _context_failure(
                    f"Could not read project '{key}': {jira._http_error_text(exc)}"
                )
            if isinstance(project_read, BaseException):
                raise project_read
            project = project_read

            if isinstance(types_read, httpx.HTTPError):
                return _context_failure(f"Could not list issue types for '{key}': {types_read}")
            if isinstance(types_read, BaseException):
                raise types_read
            project.issue_types = types_read

            # Story and Sub-task are needed by every breakdown, whatever its
            # size, so a mismatch is knowable now. Checking here avoids a whole
            # decomposition — and a model call — for a broken configuration.
            # The Epic type is only needed for Medium/Large, which is not known
            # until gate 3, so that check stays in create_jira_issues.
            types = jira.resolve_issue_types(project.issue_types)
            always_needed = [types["story"], types["subtask"]]
            missing_now = jira.missing_issue_types(always_needed, project.issue_types)
            if missing_now:
                return _context_failure(
                    f"Jira project '{key}' does not offer the required issue "
                    f"type(s): {', '.join(missing_now)}. Available: "
                    f"{', '.join(project.issue_types) or 'none'}. "
                    f"No Jira issues were created."
                )

            # --- existing tickets, for context and overlap ----------------
            unavailable: list[str] = []
            if isinstance(existing_read, httpx.HTTPError):
                existing = []
                unavailable.append(f"Could not read existing tickets for context: {existing_read}")
            elif isinstance(existing_read, BaseException):
                raise existing_read
            else:
                existing, unavailable = existing_read
                notes.append(f"Compared against {len(existing)} existing ticket(s) in {key}.")

            # --- an existing epic, when one was supplied ------------------
            epic_ctx = None
            if epic_key:
                if isinstance(epic_read, jira.EpicValidationError):
                    return _context_failure(str(epic_read))
                if isinstance(epic_read, httpx.HTTPError):
                    return _context_failure(f"Could not read epic '{epic_key}': {epic_read}")
                if isinstance(epic_read, BaseException):
                    raise epic_read
                epic_ctx = epic_read
                notes.append(
                    f"New stories will be added under existing epic {epic_ctx.key} "
                    f"({len(epic_ctx.child_stories)} story/stories already on it)."
                )

            # --- the active sprint, when sprint placement was chosen ------
            sprint = None
            if want is SprintPlacement.CURRENT_SPRINT:
                if isinstance(sprint_read, jira.SprintUnavailable):
                    # The requester asked for "the current sprint" and Jira
                    # cannot say which one that is. That is a question for them,
                    # not a misconfiguration, so it is raised as a clarification.
                    return _context_failure(
                        f"Current sprint was requested but is unavailable: {sprint_read}",
                        notes,
                        needs_clarification=True,
                    )
                if isinstance(sprint_read, httpx.HTTPError):
                    return _context_failure(
                        f"Could not resolve the active sprint for '{key}': {sprint_read}", notes
                    )
                if isinstance(sprint_read, BaseException):
                    raise sprint_read
                sprint = sprint_read
                notes.append(
                    f"New stories will be added to active sprint "
                    f"'{sprint.name}' (id {sprint.id})."
                )
    except httpx.HTTPError as exc:
        return _context_failure(f"Jira request failed: {exc}")

    logger.info(
        f"project={project.key} types={project.issue_types} "
        f"existing={len(existing)} epic={epic_ctx.key if epic_ctx else '-'} "
        f"sprint={sprint.id if sprint else '-'}"
    )
    return JiraContext(
        ok=True,
        project=project,
        existing_issues=existing,
        epic=epic_ctx,
        sprint=sprint,
        placement=want,
        notes=notes,
        unavailable=unavailable,
    ).model_dump(mode="json")


@tool()
async def validate_requirement(
    requirement: str | None = None,
    project_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether a requirement may proceed to decomposition.

    Returns one of VALIDATION_ERROR, OUT_OF_SCOPE, CLARIFICATION_REQUIRED, or
    READY_FOR_JIRA. No Jira call is ever made from here.
    """
    with stage("validate_requirement"):
        return await _validate_requirement(requirement, project_overrides)


async def _validate_requirement(
    requirement: str | None = None,
    project_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The body of :func:`validate_requirement`, timed by it."""
    logger.info("Inside the validate_requirement tool")
    text = normalise(requirement)
    context = _context_from(project_overrides)

    # Judge the REQUEST, not the request plus everything around it.
    #
    # The text handed in is the assembled bundle: the stated requirement first,
    # then the ticket, its comments, links and history. Validating the whole
    # bundle let context vouch for a request that could not stand on its own —
    # "@Aetherion What is the weather today?" passed because a nearby comment
    # said "allow users to reset passwords", and four tickets were created.
    request = strip_section_headings(requirement_core(text)) or text
    logger.info(f"validating the request itself: {request[:120]!r}")

    # Handed back to the caller as ``request_text``. The full ``text`` is the
    # assembled bundle — description, attachments, comments, history — and it
    # grows every time anyone comments on the ticket, so it can never identify
    # a request. The stated request can, and does not drift.

    # Which of the three buckets is this? The answer decides how the agent
    # replies, whether it asks anything, and whether anyone is emailed.
    bucket, bucket_questions = classify_request(request, context)
    logger.info(f"request bucket: {bucket.value}")

    if bucket is RequestBucket.NOT_A_REQUIREMENT:
        # The bucket decides how the agent *replies*; the status still says
        # precisely what was wrong, so "off topic" and "unreadable" stay
        # distinguishable to anything reading the result.
        gate = validate_input(request, context)
        status = (
            gate.status
            if gate.status in (ResultStatus.OUT_OF_SCOPE, ResultStatus.VALIDATION_ERROR)
            else ResultStatus.VALIDATION_ERROR
        )
        return {
            "status": status.value,
            "bucket": bucket.value,
            "reason": (
                gate.reason
                # Deliberately no "@" before the keyword. This text is posted
                # as a comment on the same ticket, and a platform filter that
                # routes on "body contains @Aetherion" would read the agent's
                # own reply as a fresh request and trigger it again, forever.
                or "This is not a work requirement. Reply with a description "
                "of what should be built or changed, mentioning the agent again."
            ),
            # Nothing is asked. There was no requirement to clarify, and a list
            # of questions about the project reads as though nobody looked.
            "clarifying_questions": [],
            "validation_errors": [],
            "requirement": text,
            "request_text": request,
            "error": "",
        }

    if bucket is RequestBucket.INCOMPLETE:
        # The deterministic questions are the same three for every thin request
        # ("Who is this for?…"), so "make the dashboard better" was never asked
        # WHICH dashboard (BGV scenario 4, 2026-09-24). The model writes
        # questions about this request; the fixed three remain the fallback.
        questions = bucket_questions
        try:
            asked, _ = await triage(request, context)
            if asked.status is ResultStatus.CLARIFICATION_REQUIRED and asked.clarifying_questions:
                questions = list(asked.clarifying_questions)
        except Exception as exc:  # noqa: BLE001 — generic questions beat none
            logger.info(f"Triage unavailable for specific questions, using the defaults: {exc}")
        return {
            "status": ResultStatus.CLARIFICATION_REQUIRED.value,
            "bucket": bucket.value,
            "reason": ("This describes real work, but not enough of it to break down yet."),
            "clarifying_questions": questions,
            "validation_errors": [],
            "requirement": text,
            "request_text": request,
            "error": "",
        }

    if bucket is RequestBucket.UNCERTAIN:
        # No verb the deterministic list recognises, but real, substantial
        # language. The list is hand-maintained and demonstrably incomplete, so
        # the model gets the call. If it cannot be reached, ask rather than
        # reject: a needless question costs one comment, a wrong rejection
        # loses a requirement with no trace.
        try:
            llm_verdict, generator = await triage(request, context)
        except Exception as exc:
            logger.info(f"Triage unavailable for an unrecognised-verb request: {exc}")
            return {
                "status": ResultStatus.CLARIFICATION_REQUIRED.value,
                "bucket": RequestBucket.INCOMPLETE.value,
                "reason": (
                    "This may describe real work, but it could not be read with "
                    "confidence and the language model was unavailable to check."
                ),
                "clarifying_questions": bucket_questions,
                "validation_errors": [],
                "requirement": text,
                "request_text": request,
                "error": "",
            }
        logger.info(f"Triage resolved an uncertain request as {llm_verdict.status.value}")
        resolved = (
            RequestBucket.VALID
            if llm_verdict.status is ResultStatus.READY_FOR_JIRA
            else (
                RequestBucket.INCOMPLETE
                if llm_verdict.status is ResultStatus.CLARIFICATION_REQUIRED
                else RequestBucket.NOT_A_REQUIREMENT
            )
        )
        payload = llm_verdict.model_dump(mode="json")
        # The model may say CLARIFICATION_REQUIRED without naming anything; the
        # deterministic questions are better than a generic prompt.
        if resolved is RequestBucket.INCOMPLETE and not payload.get("clarifying_questions"):
            payload["clarifying_questions"] = bucket_questions
        return {
            **payload,
            "bucket": resolved.value,
            "requirement": text,
            "request_text": request,
            "generator": generator,
            "error": "",
        }

    verdict = validate_input(request, context)
    if not verdict.may_proceed:
        logger.info(f"Deterministic gate rejected the input: {verdict.status.value}")
        return {
            **verdict.model_dump(mode="json"),
            "bucket": RequestBucket.NOT_A_REQUIREMENT.value,
            "requirement": text,
            "request_text": request,
            "error": "",
        }

    # A request that already lists its requirements is plainly work: the rules
    # above found it actionable, and the breakdown's own checks still reject
    # anything it cannot build. Asking the model again only cost time (L1,
    # 2026-09-29) — one call on almost every real build.
    listed = requirement_lines(request)
    if bucket is RequestBucket.VALID and len(listed) >= CLEAR_REQUIREMENT_LINES:
        logger.info(f"Triage skipped: {len(listed)} listed requirement lines")
        return {
            "status": ResultStatus.READY_FOR_JIRA.value,
            "bucket": bucket.value,
            "reason": f"A clear requirement with {len(listed)} listed lines.",
            "clarifying_questions": [],
            "validation_errors": [],
            "requirement": text,
            "request_text": request,
            "generator": "rules",
            "error": "",
        }

    try:
        llm_verdict, generator = await triage(request, context)
    except Exception as exc:  # only reachable when LTW_REQUIRE_LLM is set
        logger.error(f"Triage failed and the LLM is marked required: {exc}", exc_info=True)
        return {
            # A system failure, not a verdict on the request. VALIDATION_ERROR
            # plus the NOT_A_REQUIREMENT bucket told the person their valid
            # request was "not a work requirement" and emailed nobody.
            "status": ResultStatus.JIRA_CREATION_FAILED.value,
            "bucket": "",
            "reason": (
                "The AI model could not assess this request, so nothing was "
                "created. Asking again is safe."
            ),
            "clarifying_questions": [],
            "validation_errors": [str(exc)],
            "requirement": text,
            "request_text": request,
            "error": str(exc),
        }

    logger.info(f"Triage verdict: {llm_verdict.status.value} (via {generator})")
    return {
        **llm_verdict.model_dump(mode="json"),
        "bucket": bucket.value,
        "requirement": text,
        "request_text": request,
        "generator": generator,
        "error": "",
    }


async def _second_opinion(
    overlaps: list[DuplicateMatch],
    proposed: list[str],
    existing: list[ExistingIssue],
    base_url: str,
    exclude: set[str],
) -> list[DuplicateMatch]:
    """Word matches after the model's one verdict on the uncertain ones."""
    by_description = [m for m in overlaps if m.matched_on == "description"]
    settled = [m for m in overlaps if m.matched_on != "description"]
    decided = {m.proposed_title for m in settled}
    near = [
        m
        for m in jira.find_near_misses(proposed, existing, base_url=base_url, exclude_keys=exclude)
        if m.proposed_title not in decided
    ]
    candidates = by_description + near
    if not candidates:
        return overlaps

    try:
        same = await llm_same_work(
            [(m.proposed_title, m.existing_key, m.existing_summary) for m in candidates]
        )
        agreed = {i for i in range(len(by_description)) if i in same}
        confirmed = {i - len(by_description) for i in same if i >= len(by_description)}
    except Exception as exc:  # noqa: BLE001 — each kind falls back its own way
        logger.info(f"Duplicate second opinion unavailable, keeping word matches: {exc}")
        agreed, confirmed = set(range(len(by_description))), set()

    kept = [m for i, m in enumerate(by_description) if i in agreed]
    if len(kept) < len(by_description):
        logger.info(f"Model rejected {len(by_description) - len(kept)} description match(es)")
    covered = decided | {m.proposed_title for m in kept}
    semantic = [
        near[i].model_copy(update={"matched_on": "semantic"})
        for i in sorted(confirmed)
        if near[i].proposed_title not in covered
    ]
    if semantic:
        logger.info(f"{len(semantic)} of {len(near)} near miss(es) confirmed as existing work")
    return settled + kept + semantic


async def _still_there(matches: list[DuplicateMatch], project_key: str) -> list[DuplicateMatch]:
    """The matches whose ticket still exists in this project (one read each).

    A ticket deleted or moved since the index last read the whole project must
    not block new work as "already exists". An unreadable answer keeps the
    match: better to ask a person than to build a duplicate.
    """
    keys = sorted({m.existing_key for m in matches})
    base_url, email, token = jira.jira_creds()
    if not (keys and ticket_index.enabled() and base_url and email and token):
        return matches

    async def exists(client: httpx.AsyncClient, key: str) -> bool:
        try:
            resp = await client.get(f"/rest/api/3/issue/{key}", params={"fields": "project"})
        except httpx.HTTPError:
            return True
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            return True
        project = ((resp.json().get("fields") or {}).get("project") or {}).get("key", "")
        return not project_key or project.upper() == project_key.upper()

    async with jira.jira_client(base_url, email, token) as client:
        present = await asyncio.gather(*(exists(client, key) for key in keys))
    gone = {key for key, ok in zip(keys, present, strict=True) if not ok}
    if gone:
        logger.info(f"Dropped matches on tickets no longer in {project_key}: {sorted(gone)}")
        ticket_index.forget_tickets(project_key, gone)
    return [m for m in matches if m.existing_key not in gone]


async def _every_ticket(
    project_key: str, proposed: list[str], requirement: str, newest: list[ExistingIssue]
) -> list[ExistingIssue]:
    """The tickets any duplicate rule could match, from the whole project.

    Falls back to the newest tickets the context step read when the index is
    off or Jira cannot be read — exactly the behaviour before it existed.
    """
    base_url, email, token = jira.jira_creds()
    if not (ticket_index.enabled() and project_key and base_url and email and token):
        return newest
    try:
        with stage("duplicate_index", project=project_key) as fields:
            async with jira.jira_client(base_url, email, token) as client:
                index = await ticket_index.up_to_date(client, project_key, base_url)
            found = index.candidates(proposed, requirement)
            fields.update(tickets=len(index.issues), candidates=len(found))
            return found
    except httpx.HTTPError as exc:
        logger.warning(f"Ticket index unavailable ({exc}); comparing the newest tickets only")
        return newest


@tool()
async def generate_work_breakdown(
    requirement: str | None = None,
    project_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify the requirement and produce a schema-valid work breakdown.

    The returned ``breakdown`` is guaranteed to satisfy the Epic/Story/Subtask
    contracts, including "no Epic for Small". A caller may hand it straight to
    ``create_jira_issues``.
    """
    with stage("generate_work_breakdown"):
        return await _generate_work_breakdown(requirement, project_overrides)


async def _generate_work_breakdown(
    requirement: str | None = None,
    project_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The body of :func:`generate_work_breakdown`, timed by it."""
    logger.info("Inside the generate_work_breakdown tool")
    text = normalise(requirement)
    context = _context_from(project_overrides)

    if not text:
        return {
            "breakdown": None,
            "classification": "",
            "generator": "",
            "duplicate_matches": [],
            "best_match": None,
            "overlap": None,
            "overlap_is_partial": False,
            "overlap_is_full": False,
            "error": "No requirement text supplied to generate_work_breakdown.",
        }

    try:
        breakdown, generator = await build_breakdown(text, context)
    except (DecompositionError, ValidationError) as exc:
        logger.error(f"Decomposition failed: {exc}")
        return {
            "breakdown": None,
            "classification": "",
            "generator": "",
            "duplicate_matches": [],
            "best_match": None,
            "overlap": None,
            "overlap_is_partial": False,
            "overlap_is_full": False,
            "error": f"Work breakdown could not be produced: {exc}",
        }

    counts = breakdown.issue_count()

    # Compare the proposed work against tickets already in the project. This is
    # deliberately conservative token overlap: it reliably catches near-identical
    # wording, and does not pretend to recognise a paraphrase.
    existing = _existing_issues(project_overrides)

    # Issues created by this very requirement are not "duplicates" — they are
    # the same run, and the idempotency label handles them. Excluding them here
    # keeps a retry from being mistaken for a fresh conflicting request.
    # `.get("project", {})` is not enough: when the context lookup fails the key
    # is present and null, and the default never applies. That turned an
    # ordinary "project not found" into an AttributeError mid-run.
    own_project = (project_overrides or {}).get("project") or {}
    own_label = jira.idempotency_key(text, own_project.get("key", ""))

    # Only Story titles are compared. The Epic summary restates the whole
    # requirement, so including it made one long string collide with everything.
    proposed = [story.title for story in breakdown.stories]
    base_url, _, _ = jira.jira_creds()

    # Every ticket in the project, not just the newest the context step read.
    existing = await _every_ticket(own_project.get("key", ""), proposed, text, existing)
    existing = [i for i in existing if own_label not in i.labels]
    # The trigger ticket restates the requirement in its own description, so
    # comparing against it matches every proposed Story. Exclude it by key.
    trigger_key = str((project_overrides or {}).get("trigger_key") or "").strip().upper()
    exclude = {trigger_key} if trigger_key else set()

    report = (
        jira.build_overlap_report(
            proposed, existing, base_url=base_url, exclude_keys=exclude, requirement=text
        )
        if existing
        else OverlapReport(remaining_titles=proposed)
    )
    overlaps = report.matches
    closest = jira.best_overlap(proposed, existing, exclude) if existing else None

    # --- one second opinion for everything word overlap cannot decide ------
    # Two kinds of match need the model, and they are asked in ONE call (L2,
    # 2026-09-29) — they used to be two, one after the other:
    #
    # * a description match. Word containment in a long description is weak
    #   evidence, and it blocks work outright: BGV-57's "Officer ID
    #   Verification" was "covered" by a ticket about address history. It
    #   stands only if the model agrees; if the model cannot be asked, the word
    #   match stands, as it always did.
    # * a near miss. Jaccard scores "Let drivers record a rest break" against
    #   "Allow drivers to log a rest break" at 0.60 — the same work, below the
    #   0.75 line. "Send email notifications" and "Send SMS notifications" also
    #   score 0.60 and are different work. Only meaning separates them. If the
    #   model cannot be asked, a near miss is no match — as before it existed.
    if existing and jira.adjudication_enabled():
        overlaps = await _second_opinion(overlaps, proposed, existing, base_url, exclude)

    # A match is only real if that ticket is still in the project: the index
    # learns about deleted or moved tickets only at its daily full re-read.
    overlaps = await _still_there(overlaps, own_project.get("key", ""))
    covered = {m.proposed_title for m in overlaps}
    report = OverlapReport(
        matches=overlaps,
        covered_titles=[t for t in proposed if t in covered],
        remaining_titles=[t for t in proposed if t not in covered],
    )

    logger.info(
        f"classification : {breakdown.classification.value} | "
        f"epics={counts['epics']} stories={counts['stories']} "
        f"subtasks={counts['subtasks']} | generator={generator} | "
        f"overlaps={len(overlaps)} | closest={closest.score if closest else '-'}"
    )
    return {
        "breakdown": breakdown.model_dump(mode="json"),
        "classification": breakdown.classification.value,
        "counts": counts,
        "generator": generator,
        # What the self-review gate could not fix in its one regeneration.
        "quality_notes": list(getattr(breakdown, "_quality_notes", []) or []),
        # Requirement line -> the ticket that delivers it (for the PDF).
        "coverage": {
            str(k): str(v) for k, v in (getattr(breakdown, "_coverage", {}) or {}).items()
        },
        "duplicate_matches": [m.model_dump(mode="json") for m in overlaps],
        "best_match": closest.model_dump(mode="json") if closest else None,
        "overlap": report.model_dump(mode="json"),
        "overlap_is_partial": report.is_partial,
        "overlap_is_full": report.is_full,
        "error": "",
    }


def _config_failure(reason: str, key_label: str = "") -> dict[str, Any]:
    """A refusal to touch Jira at all — nothing was created, retry is safe."""
    logger.error(f"Jira creation refused: {reason}")
    return JiraResult(
        status=ResultStatus.JIRA_CREATION_FAILED,
        failed_issue="preflight",
        failure_reason=reason,
        retry_safe=True,
        idempotency_key=key_label,
        summary="No Jira issues were created.",
    ).model_dump(mode="json")


async def _is_related(client: httpx.AsyncClient, source_key: str, request: str) -> bool:
    """Whether the ticket asked on is about the requested work.

    The words decide when they are clear — the ticket's summary in the request,
    or the request in its description; or no shared words at all, as with an
    "Agent test ticket". Only the unclear middle asks the model (L4,
    2026-09-29); if it cannot answer, the words decide that too.
    """
    try:
        ticket = await root_mod.read_root(client, source_key)
    except httpx.HTTPError as exc:
        # Linking to the ticket asked on is what always happened; unreadable is
        # no reason to stop doing it.
        logger.warning(f"Could not read {source_key} to judge relatedness: {exc}")
        return True
    description = jira._plain_text(ticket["description"])
    by_summary = jira.containment(ticket["summary"], request)
    by_description = jira.containment(request, description) if description else 0.0
    if by_summary >= RELATED_BY_WORDS or by_description >= RELATED_BY_WORDS:
        return True
    if by_summary == 0.0 and by_description < UNRELATED_BY_WORDS:
        logger.info(f"{source_key} shares no words with the request: not related")
        return False
    try:
        return await llm_related(source_key, ticket["summary"], description, request)
    except Exception as exc:  # noqa: BLE001 — the words decide instead
        logger.info(f"Relatedness model unavailable ({exc}); not related by the words")
        return False


_ARTICLE = {"epic": "an", "story": "a", "task": "a", "bug": "a"}


def _root_summary(change: RootChange, plan: root_mod.RootPlan, result: JiraResult) -> str:
    """What happened to the root and under it, in the reply's words."""
    made = set(result.created_keys)
    stories = [r for r in result.stories if r.key in made]
    subtasks = [r for r in result.subtasks if r.key in made]
    reused = len(result.reused_keys)

    changed = change.type_before.lower() != change.type_after.lower()
    what = f"{_ARTICLE[plan.role]} {change.type_after}"
    if change.priority:
        what += f" with priority {change.priority}"
    text = (
        f"{change.key} is now {what} (it was {_ARTICLE.get(change.type_before.lower(), 'a')} "
        f"{change.type_before}), because {plan.reason}."
        if changed
        else f"{change.key} stays {what}, because {plan.reason}."
    )
    if stories or subtasks:
        parts = []
        if stories:
            parts.append(f"{len(stories)} Stor{'y' if len(stories) == 1 else 'ies'}")
        if subtasks:
            parts.append(f"{len(subtasks)} Sub-task{'' if len(subtasks) == 1 else 's'}")
        text += f" Created under it: {' and '.join(parts)}."
    elif plan.role == root_mod.BUG:
        text += " No child tickets were needed."
    elif plan.role == root_mod.TASK and not plan.create_subtasks:
        text += " It is one step, so no Sub-tasks were needed."
    if reused:
        text += f" {reused} ticket(s) were already under it and were reused, not created again."
    if plan.role == root_mod.EPIC:
        text += " No separate Epic was created."
    if change.summary_after != change.summary_before:
        text += f' Its summary is now "{change.summary_after}".'
    text += (
        " Its original summary and description are kept under "
        f'"{root_mod.ORIGINAL_HEADING}" at the end of its description.'
    )
    return text


async def _build_on_root(
    client: httpx.AsyncClient,
    base_url: str,
    project_key: str,
    model: WorkBreakdown,
    key_label: str,
    root_key: str,
    request: str,
    available: list[str],
    types: dict[str, str],
) -> dict[str, Any]:
    """Make ``root_key`` the top of the hierarchy and build under it.

    Order: every check first (nothing written if one fails), then the type
    change (nothing else written if Jira refuses it), then the children, then
    the summary, description and labels — last, so a run that stops part-way
    leaves the person's description as it was, and asking again continues.
    """
    try:
        root = await root_mod.read_root(client, root_key)
        allowed_to_edit = await root_mod.can_edit(client, project_key)
    except httpx.HTTPError as exc:
        return _config_failure(
            f"{root_key} could not be read before changing it: {exc}. Nothing was changed.",
            key_label,
        )

    plan = root_mod.plan_root(model, root["type"], request)
    names = root_mod.type_names(available, types)
    reason = root_mod.refusal(plan, root, names)
    if reason:
        return _config_failure(reason, key_label)
    needed = ([types["story"]] if plan.create_stories else []) + (
        [types["subtask"]] if plan.create_stories or plan.create_subtasks else []
    )
    missing = jira.missing_issue_types(needed, available)
    if missing:
        return _config_failure(
            f"Jira project '{project_key}' does not offer the issue type(s) "
            f"{', '.join(missing)}. {root_key} was not changed.",
            key_label,
        )
    if not allowed_to_edit:
        return _config_failure(
            f"This account may not edit issues in {project_key}, so {root_key} was not "
            f"changed and nothing was created.",
            key_label,
        )

    target = names[plan.role]
    change = RootChange(
        key=root_key,
        url=f"{base_url.rstrip('/')}/browse/{root_key}",
        type_before=root["type"],
        type_after=target,
        summary_before=root["summary"],
        summary_after=root["summary"],
    )
    if root["type"].lower() != target.lower():
        try:
            await root_mod.change_type(client, root_key, target)
        except httpx.HTTPStatusError as exc:
            return _config_failure(
                f"Jira refused to change {root_key} from {root['type']} to {target}: "
                f"{jira._http_error_text(exc)}. Nothing was changed. Change its type by "
                f"hand (••• → Change type → {target}) and ask again.",
                key_label,
            )
        logger.info(f"{root_key}: {root['type']} -> {target} ({plan.reason})")

    result = await jira.build_under_root(
        client,
        base_url,
        project_key,
        model,
        key_label,
        root_key,
        create_stories=plan.create_stories,
        create_subtasks=plan.create_subtasks,
        types=types,
    )
    result.root = change
    if result.status is not ResultStatus.JIRA_CREATED:
        result.failure_reason = (
            f"{root_key} is now {_ARTICLE[plan.role]} {target}, but building under it "
            f"stopped: {result.failure_reason} Asking again continues from there."
        )
        return result.model_dump(mode="json")

    summary_after = root_mod.new_summary(model, plan, root["summary"])
    try:
        change.notes = await root_mod.rewrite(
            client,
            root_key,
            summary=summary_after,
            description=root_mod.root_description(
                model, plan, root["description"], root["summary"]
            ),
            labels=[key_label, jira.processed_label()],
            priority=plan.priority,
        )
    except httpx.HTTPError as exc:
        result.status = ResultStatus.JIRA_CREATION_FAILED
        result.failed_issue = f"Summary and description of {root_key}"
        result.failure_reason = (
            f"The tickets were created, but {root_key}'s summary and description could "
            f"not be updated: {exc}. Asking again finishes it without creating anything twice."
        )
        return result.model_dump(mode="json")
    change.summary_after = summary_after
    change.priority = plan.priority if not change.notes else ""
    result.summary = _root_summary(change, plan, result) + "".join(f" {n}." for n in change.notes)
    logger.info(result.summary)
    return result.model_dump(mode="json")


@tool()
async def create_jira_issues(
    breakdown: dict[str, Any] | None = None,
    requirement: str | None = None,
    project_key: str | None = None,
    correlation_id: str | None = None,
    extra_labels: list[str] | None = None,
    existing_epic_key: str | None = None,
    sprint_id: int | None = None,
    root_key: str | None = None,
    idempotency_basis: str | None = None,
    source_key: str | None = None,
    requested_by: str | None = None,
    available_types: list[str] | None = None,
) -> dict[str, Any]:
    """Create the Jira hierarchy for an already-validated breakdown.

    Refuses before any write when the breakdown fails schema validation, the
    Jira configuration is incomplete, the project key is invalid, or a required
    issue type is unavailable in that project. Re-running with the same
    requirement finds the previous issues by idempotency label and returns them
    rather than creating duplicates.

    ``root_key`` names the ticket that holds the requirement itself: it becomes
    the top of the hierarchy (see :mod:`src.jira.root`) and no Epic is created
    beside it. Otherwise the new work is linked to ``source_key`` only when that
    ticket is really related; when it is not, the new ticket says where it was
    requested (``requested_by``) instead.
    """
    with stage(
        "create_jira_issues",
        project=normalise(project_key).upper() or "-",
        root=normalise(root_key).upper() or "-",
    ):
        return await _create_jira_issues(
            breakdown,
            requirement,
            project_key,
            correlation_id,
            extra_labels,
            existing_epic_key,
            sprint_id,
            root_key,
            idempotency_basis,
            source_key,
            requested_by,
            available_types,
        )


async def _create_jira_issues(
    breakdown: dict[str, Any] | None = None,
    requirement: str | None = None,
    project_key: str | None = None,
    correlation_id: str | None = None,
    extra_labels: list[str] | None = None,
    existing_epic_key: str | None = None,
    sprint_id: int | None = None,
    root_key: str | None = None,
    idempotency_basis: str | None = None,
    source_key: str | None = None,
    requested_by: str | None = None,
    available_types: list[str] | None = None,
) -> dict[str, Any]:
    """The body of :func:`create_jira_issues`, timed by it."""
    logger.info("Inside the create_jira_issues tool")

    if jira.read_only():
        return _config_failure(
            "This deployment is in read-only mode (LTW_READ_ONLY). The work "
            "breakdown was produced but no Jira issues were created."
        )

    if not breakdown:
        return _config_failure("No work breakdown supplied; nothing to create.")

    try:
        # Same allowance as at generation time, or a breakdown that legitimately
        # quotes a filename from the request would be refused at write time.
        with allow_source_files_from(requirement or ""):
            model = WorkBreakdown.model_validate(breakdown)
    except ValidationError as exc:
        return _config_failure(f"Work breakdown failed schema validation: {exc}")

    base_url, email, token = jira.jira_creds()
    logger.info(f"JIRA_BASE_URL: {base_url or '<empty>'}")
    logger.info(f"JIRA_EMAIL: {email or '<empty>'}")
    logger.info(f"JIRA_API_TOKEN: {jira._mask(token)}")

    if not (base_url and email and token):
        return _config_failure(
            "Missing JIRA_BASE_URL, JIRA_EMAIL, or JIRA_API_TOKEN. " "No Jira issues were created."
        )

    key = jira.target_project_key(normalise(project_key))
    if not key:
        return _config_failure(
            "No Jira project key provided (set the project_key trigger or "
            "JIRA_PROJECT_KEY). No Jira issues were created."
        )

    allowed = jira.allowed_project_keys()
    if allowed and key not in allowed:
        return _config_failure(
            f"Project '{key}' is not on this deployment's allowed list "
            f"({', '.join(allowed)}). No Jira issues were created."
        )

    # What identifies this request. ``requirement`` is the assembled bundle —
    # ticket description, attachments, every comment, field history — and it
    # changes every time anybody comments, so hashing it would give the same
    # request a different label on every run. ``idempotency_basis`` is the
    # stated request alone, which is stable and is what actually identifies
    # the work being asked for.
    basis = normalise(idempotency_basis) or normalise(requirement) or model.analysis
    root = normalise(root_key).upper()
    # A root keeps one label for life: its description is rewritten on the
    # first build, so a label hashed from the request would change with it.
    key_label = jira.idempotency_key(
        basis, key, root_mod.root_basis(root) if root else normalise(correlation_id)
    )
    logger.info(f"idempotency_key : {key_label} (basis {basis[:80]!r})")

    try:
        async with jira.jira_client(base_url, email, token) as client:
            # --- Preflight: project reachable, issue types present, no duplicate.
            try:
                # Read once per run: the context step already listed them.
                available = available_types or await jira.available_issue_types(client, key)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403):
                    return _config_failure(
                        f"Jira authentication failed: {jira._http_error_text(exc)}",
                        key_label,
                    )
                if exc.response.status_code == 404:
                    return _config_failure(
                        f"Jira project '{key}' was not found or is not visible to "
                        f"this account. No Jira issues were created.",
                        key_label,
                    )
                return _config_failure(
                    f"Jira preflight failed: {jira._http_error_text(exc)}", key_label
                )

            # The names THIS project uses ("Sub-task" or "Subtask").
            types = jira.resolve_issue_types(available)

            if root:
                return await _build_on_root(
                    client, base_url, key, model, key_label, root, basis, available, types
                )
            needed = [types["story"], types["subtask"]] + (
                [types["epic"]] if model.epic is not None else []
            )
            missing = jira.missing_issue_types(needed, available)
            if missing:
                return _config_failure(
                    f"Jira project '{key}' does not offer the required issue "
                    f"type(s): {', '.join(missing)}. Available: "
                    f"{', '.join(available) or 'none'}. No Jira issues were created.",
                    key_label,
                )

            try:
                existing = await jira.find_existing(client, key, key_label)
            except httpx.HTTPError as exc:
                # A duplicate check that cannot run is a reason to stop, not to
                # risk a second copy of the whole hierarchy.
                return _config_failure(
                    f"Duplicate check failed, so creation was not attempted: {exc}",
                    key_label,
                )

            if existing:
                logger.info(f"Found {len(existing)} existing issue(s) for {key_label}; reusing.")
                return jira._reuse(base_url, key_label, existing).model_dump(mode="json")

            # Decided before creating: an unrelated ticket gets no link, so the
            # origin has to be written into the new ticket's description.
            source = normalise(source_key).upper()
            related = bool(source) and await _is_related(client, source, basis)
            origin_note = (
                f"Requested in a comment on {source}"
                + (f" by {normalise(requested_by)}" if normalise(requested_by) else "")
                + "."
                if source and not related
                else ""
            )

            result = await jira.create_hierarchy(
                client,
                base_url,
                key,
                model,
                key_label,
                extra_labels,
                existing_epic_key=normalise(existing_epic_key).upper(),
                types=types,
                origin_note=origin_note,
            )

            # Sprint placement happens after creation: an issue must exist
            # before it can be moved into a sprint. Stories carry their own
            # subtasks across, so only story keys are sent.
            if sprint_id and result.status is ResultStatus.JIRA_CREATED:
                story_keys = [ref.key for ref in result.stories]
                try:
                    await jira.add_to_sprint(client, int(sprint_id), story_keys)
                    result.placement = SprintPlacement.CURRENT_SPRINT
                    result.summary += (
                        f" Added {len(story_keys)} story/stories to the active sprint."
                    )
                    logger.info(f"Added {story_keys} to sprint {sprint_id}")
                except httpx.HTTPError as exc:
                    # The tickets exist and are correct; only placement failed.
                    # That is worth reporting, not worth failing the run over.
                    reason = (
                        jira._http_error_text(exc)
                        if isinstance(exc, httpx.HTTPStatusError)
                        else str(exc)
                    )
                    result.sprint_error = (
                        f"Issues were created but could not be added to the sprint: "
                        f"{reason}. They remain in the backlog."
                    )
                    logger.error(result.sprint_error)
            # Link the new top of the hierarchy back to the ticket it was asked
            # on — only when that ticket is about this work.
            if related and result.status is ResultStatus.JIRA_CREATED:
                tops = (
                    [result.epic.key]
                    if result.epic and not result.epic_reused
                    else [ref.key for ref in result.stories]
                )
                for top in (k for k in tops if k and k != source):
                    try:
                        await jira.link_related(client, source, top)
                    except httpx.HTTPError as exc:
                        # The work exists and is correct; only the link failed.
                        logger.warning(f"Could not link {top} to {source}: {exc}")
    except httpx.HTTPError as exc:
        return _config_failure(f"Jira request failed: {exc}", key_label)

    logger.info(f"jira_status : {result.status.value} | keys={result.created_keys}")
    return result.model_dump(mode="json")


@tool()
async def jira_configuration_status() -> dict[str, Any]:
    """Report whether Jira is configured, without creating anything.

    Useful from the local test form to confirm credentials before a real run.
    """
    base_url, email, token = jira.jira_creds()
    key = jira.target_project_key()
    return {
        "base_url_set": bool(base_url),
        "email_set": bool(email),
        "token_set": bool(token),
        "project_key": key,
        "issue_types": jira.issue_type_names(),
        "llm_model": os.environ.get("LTW_LLM_MODEL", "gpt-4o"),
        "allowed_project_keys": list(jira.allowed_project_keys()) or ["<any>"],
        "read_only": jira.read_only(),
        "ready": bool(base_url and email and token and key),
    }


# --------------------------------------------------------------------------
# Webhook ingestion, notification and write-back
# --------------------------------------------------------------------------


def _issue_failure(reason: str, issue_key: str = "") -> dict[str, Any]:
    """An issue that could not be read. Nothing downstream should run."""
    logger.error(f"Could not read issue {issue_key or '<none>'}: {reason}")
    return SourceIssue(ok=False, error=reason, key=issue_key).model_dump(mode="json")


def _pick_trigger_comment(
    comments: list[CommentInfo], answered: set[str], wanted_id: str = ""
) -> CommentInfo | None:
    """The comment asking for work, or None.

    A request is a comment that mentions the keyword, was not written by this
    agent, and has not already been answered. When the webhook names a specific
    comment that one is used; otherwise the most recent unanswered request
    wins, so a ticket can carry many requests over its life.
    """
    candidates = [
        c
        for c in comments
        if jira.mentions_trigger(c.body)
        and not jira.is_agent_comment(c.body)
        and str(c.id) not in answered
    ]
    if wanted_id:
        return next((c for c in candidates if str(c.id) == str(wanted_id)), None)
    return candidates[-1] if candidates else None


async def _issue_and_answered(
    client: httpx.AsyncClient, key: str, check_answered: bool
) -> tuple[dict[str, Any], set[str]]:
    """The issue and the comments already answered on it, fetched together.

    A failure to read the issue is raised, exactly as before, so the caller's
    404/401/403 handling is unchanged. The answered list never fails the read.
    """

    async def none_answered() -> set[str]:
        return set()

    issue, answered = await asyncio.gather(
        jira.fetch_issue(client, key),
        jira.answered_comment_ids(client, key) if check_answered else none_answered(),
        return_exceptions=True,
    )
    if isinstance(issue, BaseException):
        raise issue
    return issue, answered if isinstance(answered, set) else set()


async def _read_attachments(
    client: httpx.AsyncClient, fields: dict[str, Any], reading: bool
) -> tuple[list[AttachmentText], list[str]]:
    """The ticket's attachments, read in parallel — or none, when not reading."""
    items = fields.get("attachment") or []
    if not (items and reading):
        return [], []
    with stage("read_attachments", files=min(len(items), attachments_mod.max_files())):
        return await attachments_mod.read_all(
            items, lambda url: jira.download_attachment(client, url)
        )


async def _find_root_built(
    client: httpx.AsyncClient,
    key: str,
    project_key: str,
    base_url: str,
    labels: list[str],
    reading: bool,
) -> dict[str, Any] | None:
    """What an earlier build made under this ticket as its root, or None.

    A ticket that became a root keeps one label for life, on itself and on
    everything built under it. Children with it, on a root already marked
    processed, mean this was asked and built.
    """
    if not (reading and jira.processed_label() in labels):
        return None
    root_label = jira.idempotency_key("", project_key, root_mod.root_basis(key))
    try:
        built = [
            i
            for i in await jira.find_existing(client, project_key, root_label)
            if i.get("key") != key
        ]
    except httpx.HTTPError as exc:
        logger.warning(f"Could not look for tickets built under {key}: {exc}")
        return None
    return jira._reuse(base_url, root_label, built).model_dump(mode="json") if built else None


async def _read_confluence(
    client: httpx.AsyncClient, key: str, scan: list[str], base_url: str, reading: bool
) -> tuple[list[ConfluencePage], list[str]]:
    """Linked Confluence pages — supporting material, never worth failing over."""
    if not (reading and confluence_mod.enabled()):
        return [], []
    with stage("read_confluence") as fields:
        try:
            remote_urls = await confluence_mod.fetch_remote_link_urls(client, key)
            pages, notes = await confluence_mod.fetch_linked_pages(
                client, scan, base_url, remote_urls
            )
            fields["pages"] = len(pages)
            return pages, notes
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Confluence lookup failed for {key}: {exc}")
            return [], [f"Linked Confluence pages could not be read: {exc}"]


@tool()
async def read_jira_issue(
    issue_key: str | None = None,
    require_trigger: bool | None = None,
    skip_if_processed: bool | None = None,
    comment_id: str | None = None,
) -> dict[str, Any]:
    """Read everything on one Jira issue and assemble it into a requirement.

    Pulls the description, attachments (text extracted), comments, field
    history, work logs and linked work items in as few calls as Jira allows,
    and derives the project key from the issue itself rather than from a form.

    Args:
        issue_key: The issue that triggered the run, e.g. ``KS-12``.
        require_trigger: When true (default), the run only proceeds if the
            trigger keyword appears in the summary, description or a comment.
        skip_if_processed: When true (default), an issue already carrying the
            processed label is reported as such so the caller can stop. This is
            what stops a webhook from reprocessing its own write-back comment.

    Returns:
        A ``SourceIssue`` dict. ``ok`` is false when the issue could not be
        read; ``trigger_matched`` and ``already_processed`` carry the gate
        decisions without making them.
    """
    with stage("read_jira_issue", issue=normalise(issue_key).upper() or "-"):
        return await _read_jira_issue(issue_key, require_trigger, skip_if_processed, comment_id)


async def _read_jira_issue(
    issue_key: str | None = None,
    require_trigger: bool | None = None,
    skip_if_processed: bool | None = None,
    comment_id: str | None = None,
) -> dict[str, Any]:
    """The body of :func:`read_jira_issue`, timed by it."""
    logger.info("Inside the read_jira_issue tool")

    key = normalise(issue_key).upper()
    want_trigger = True if require_trigger is None else bool(require_trigger)
    skip_processed = True if skip_if_processed is None else bool(skip_if_processed)

    if not key:
        return _issue_failure("No Jira issue key supplied. Nothing to read.")

    base_url, email, token = jira.jira_creds()
    if not (base_url and email and token):
        return _issue_failure("Missing JIRA_BASE_URL, JIRA_EMAIL, or JIRA_API_TOKEN.", key)

    notes: list[str] = []

    try:
        async with jira.jira_client(base_url, email, token) as client:
            try:
                payload, answered = await _issue_and_answered(client, key, skip_processed)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    if not await jira.is_authenticated(client):
                        return _issue_failure(
                            "Jira authentication failed. Check JIRA_EMAIL and "
                            "JIRA_API_TOKEN — the credentials were rejected.",
                            key,
                        )
                    return _issue_failure(
                        f"Issue '{key}' was not found, or is not visible to this account.", key
                    )
                if exc.response.status_code in (401, 403):
                    return _issue_failure(
                        f"This account is not permitted to read '{key}': "
                        f"{jira._http_error_text(exc)}",
                        key,
                    )
                return _issue_failure(f"Could not read '{key}': {jira._http_error_text(exc)}", key)

            fields = payload.get("fields") or {}
            project = fields.get("project") or {}
            summary = fields.get("summary", "") or ""
            description = jira._plain_text(fields.get("description"))

            comments = jira.parse_comments(fields)
            history = jira.parse_history(payload)
            worklogs = jira.parse_worklogs(fields)
            links = jira.parse_links(fields)
            labels = [str(x) for x in (fields.get("labels") or [])]

            # --- which comment is asking for work? ------------------------
            # A request is a comment mentioning the keyword that this agent did
            # not write and has not already answered. Tracking it per comment
            # (rather than per ticket) lets one ticket carry many requests.
            wanted = normalise(comment_id)
            trigger = _pick_trigger_comment(comments, answered, wanted)

            matched = trigger is not None
            processed = bool(wanted and wanted in answered)

            if processed:
                notes.append("This request has already been answered.")
            elif not matched:
                notes.append(
                    f"No unanswered comment on this issue mentions " f"'{jira.trigger_keyword()}'."
                )

            # A manual run supplies the requirement directly and has no comment.
            if not want_trigger:
                matched = True

            # "Add the stories to the current sprint" is a destination, not
            # work. Read it out of whatever carries the request.
            placement_choice, _ = detect_placement(trigger.body if trigger else description)

            # --- attachments, earlier builds, Confluence: together ------------
            # None depends on another, so they are read at the same time. Each
            # reports its own failure as a note, so one cannot sink the others.
            reading = matched and not processed
            (attachments, attachment_notes), root_built, (confluence_pages, page_notes) = (
                await asyncio.gather(
                    _read_attachments(client, fields, reading),
                    _find_root_built(
                        client, key, project.get("key", ""), base_url, labels, reading
                    ),
                    _read_confluence(
                        client,
                        key,
                        [trigger.body if trigger else "", description, summary],
                        base_url,
                        reading,
                    ),
                )
            )
            notes.extend(attachment_notes)
            notes.extend(page_notes)
    except httpx.HTTPError as exc:
        return _issue_failure(f"Jira request failed: {exc}", key)

    source = SourceIssue(
        ok=True,
        key=payload.get("key", key),
        project_key=str(project.get("key", "")).upper(),
        project_name=str(project.get("name", "")),
        summary=summary,
        description=description,
        issue_type=(fields.get("issuetype") or {}).get("name", "") or "",
        status=(fields.get("status") or {}).get("name", "") or "",
        reporter=jira._person(fields.get("reporter")),
        created=str(fields.get("created", ""))[:19],
        labels=labels,
        attachments=attachments,
        confluence_pages=confluence_pages,
        comments=comments,
        history=history,
        worklogs=worklogs,
        linked_issues=links,
        trigger_matched=matched,
        already_processed=processed,
        notes=notes,
        requested_placement=SprintPlacement(placement_choice),
        trigger_comment_id=str(trigger.id) if trigger else "",
        trigger_comment_author=trigger.author if trigger else "",
        trigger_comment_created=trigger.created[:10] if trigger else "",
        trigger_comment_body=trigger.body if trigger else "",
        trigger_is_pointer=bool(trigger and is_pointer_comment(trigger.body)),
        is_subtask=bool((fields.get("issuetype") or {}).get("subtask")),
        root_built=root_built,
    )
    source.ticket_is_root = source.trigger_is_pointer and not has_url(source.trigger_comment_body)

    # Decided here, not in the agent: each of these needs the trigger keyword,
    # which is read from the environment — forbidden inside a workflow.
    source.trigger_is_question = bool(
        source.trigger_comment_body and is_question_comment(source.trigger_comment_body)
    )
    source.unread_links = find_unread_links(source)
    source.request_is_only_link = request_is_only_a_pointer(source)

    source.conflicts = find_conflicts(source)
    if source.conflicts:
        logger.info(f"{len(source.conflicts)} ticket/page conflict(s) on {source.key}")

    requirement_text, budget_notes = assemble_requirement(source)
    source.requirement_text = requirement_text
    source.notes = notes + budget_notes

    if source.trigger_is_question:
        # After assembly, so the model sees attachments and linked pages too.
        try:
            source.explanation = await explain_issue(
                strip_trigger_mentions(source.trigger_comment_body, jira.trigger_keyword()),
                requirement_text,
            )
        except Exception as exc:  # noqa: BLE001 — a quoted ticket beats no answer
            logger.warning(f"Model explanation unavailable, quoting the ticket: {exc}")
            source.explanation = describe_issue(source)

    logger.info(
        f"Read {source.key} from project {source.project_key}: "
        f"{source.source_counts()} | trigger_matched={matched} | "
        f"already_processed={processed} | requirement_chars={len(requirement_text)}"
    )
    return source.model_dump(mode="json")


@tool()
async def read_uploaded_files(
    uploads: dict[str, Any] | None = None, team_id: str | None = None
) -> dict[str, Any]:
    """Read the files uploaded on the agent's Aetherion form (a spec, a transcript …).

    ``uploads`` carries the payload's ``uploaded_files`` (and the field's own
    name, if sent). Returns each file's text or why it could not be read, and
    the requirement section they make.
    """
    keys = uploads_mod.upload_keys(uploads or {})
    with stage("read_uploaded_files", files=len(keys)):
        files, notes = await uploads_mod.read_uploads(keys, team_id)
    logger.info(
        f"Uploaded files: {[f.filename for f in files]} "
        f"(read: {sum(1 for f in files if f.text.strip())})"
    )
    return {
        "files": [f.model_dump(mode="json") for f in files],
        "notes": notes,
        "section": uploads_mod.section(files),
    }


# --------------------------------------------------------------------------
# GENERATE_LOCAL_PDF: the breakdown as an attached PDF, nothing else changed
# --------------------------------------------------------------------------


def local_pdf_enabled() -> bool:
    """GENERATE_LOCAL_PDF: a build ends in a PDF attached to the ticket.

    Nothing in Jira is created, converted, labelled or edited; the reply says
    what the PDF proposes. False (the default): the tickets are built.
    """
    return env_bool("GENERATE_LOCAL_PDF", default=False)


@tool()
async def delivery_mode() -> dict[str, Any]:
    """How a build ends: in Jira tickets, or in an attached PDF."""
    return {"local_pdf": local_pdf_enabled()}


@tool()
async def generate_breakdown_pdf(request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render the validated breakdown to a PDF and attach it — change nothing else.

    ``request`` carries what the build already knows: ``breakdown``,
    ``requirement``, ``request``, ``issue_key``, ``issue_summary``,
    ``project_key``, ``root_key``, ``agent_version`` and the ``report`` details
    shown in the document.
    """
    with stage("generate_breakdown_pdf"):
        return await _generate_breakdown_pdf(request or {})


async def _generate_breakdown_pdf(request: dict[str, Any]) -> dict[str, Any]:
    def failed(reason: str) -> dict[str, Any]:
        return {"status": ResultStatus.JIRA_CREATION_FAILED.value, "reason": reason}

    # A manual run (a requirement typed in, no ticket) is named after its project.
    project = normalise(request.get("project_key")).upper()
    issue_key = normalise(request.get("issue_key")).upper() or (
        f"{project}-REQUEST" if project else ""
    )
    try:
        with allow_source_files_from(str(request.get("requirement") or "")):
            model = WorkBreakdown.model_validate(request.get("breakdown") or {})
    except ValidationError as exc:
        return failed(f"Work breakdown failed schema validation: {exc}")
    base_url, email, token = jira.jira_creds()
    if not (issue_key and base_url and email and token):
        return failed("Missing the project key or the Jira credentials; no PDF was made.")

    report = request.get("report") or {}
    try:
        async with jira.jira_client(base_url, email, token) as client:
            root = await _root_preview(client, model, request)
            if isinstance(root, str):
                return failed(root)
            existing_epic = "" if root else _existing_epic(request)
            doc = pdf.BreakdownDocument(
                issue_key=issue_key,
                issue_summary=str(request.get("issue_summary") or ""),
                project_key=normalise(request.get("project_key")).upper(),
                generated_at=f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
                agent_version=str(request.get("agent_version") or ""),
                breakdown=model,
                root=root,
                related_keys=list(report.get("related_keys") or []),
                sources_read=list(report.get("sources_read") or []),
                sources_missing=list(report.get("sources_missing") or []),
                requirement_lines=requirement_lines(str(request.get("request") or "")),
                coverage=dict(report.get("coverage") or {}),
                quality_notes=list(report.get("quality_notes") or []),
                review_questions=list(request.get("review_questions") or []),
                closest_match=report.get("closest_match"),
                placement=str(report.get("placement") or "backlog"),
                existing_epic=existing_epic,
            )
            name = pdf.file_name(issue_key, model)
    except httpx.HTTPError as exc:
        return failed(f"{issue_key} could not be read for the PDF: {exc}. Nothing was changed.")

    root_is_epic = bool(root and root.type_after.lower() == "epic")
    proposed = pdf.numbered(model, root_is_epic=root_is_epic, existing_epic=existing_epic)
    counts = model.issue_count()
    becomes = (
        f" If built, {root.key} would become {_ARTICLE.get(root.type_after.lower(), 'a')} "
        f"{root.type_after} (it is {_ARTICLE.get(root.type_before.lower(), 'a')} "
        f"{root.type_before} now)."
        if root and root.type_after.lower() != root.type_before.lower()
        else ""
    )
    what = (
        f"The breakdown proposes {counts['stories']} "
        f"Stor{'y' if counts['stories'] == 1 else 'ies'} and {counts['subtasks']} "
        f"Sub-task{'' if counts['subtasks'] == 1 else 's'}.{becomes}"
    )
    questions = [f"{st.title}: {q}" for st in model.stories for q in st.open_questions] + [
        f"From the review: {q}" for q in request.get("review_questions") or []
    ]
    emailed = await notifier.send_breakdown_pdf(
        issue_key=issue_key,
        issue_summary=doc.issue_summary,
        summary=what,
        proposed=proposed,
        questions=questions,
        filename=name,
        data=pdf.render_pdf(doc),
    )
    if not emailed.sent:
        return failed(
            f"The breakdown PDF could not be emailed: {emailed.error}. No Jira tickets were "
            "created or changed; asking again is safe."
        )
    to = ", ".join(emailed.recipients)
    return {
        "status": ResultStatus.PLANNED.value,
        "headline": "Work breakdown emailed as a PDF — no Jira changes made",
        "emailed_to": emailed.recipients,
        "attachment": name,
        "summary": (
            f"{pdf.MODE_LINE}. {what} The full breakdown — every ticket, its criteria and the "
            f"sources read — was emailed to {to} as {name}."
        ),
        "proposed": proposed,
    }


def _existing_epic(request: dict[str, Any]) -> str:
    """The Epic the form named, as the PDF shows it: "BGV-69 — Candidate Address Verification".

    Only without a root, as in creation: a root is the top of its own hierarchy.
    """
    epic = request.get("existing_epic") or {}
    key = normalise(epic.get("key")).upper()
    summary = normalise(epic.get("summary"))
    return f"{key} — {summary}" if key and summary else key


async def _root_preview(
    client: httpx.AsyncClient, model: WorkBreakdown, request: dict[str, Any]
) -> pdf.RootPreview | str | None:
    """What building would do to the root — read only — or why it would refuse."""
    root_key = normalise(request.get("root_key")).upper()
    if not root_key:
        return None
    root = await root_mod.read_root(client, root_key)
    project_key = normalise(request.get("project_key")).upper()
    available = await jira.available_issue_types(client, project_key)
    names = root_mod.type_names(available, jira.resolve_issue_types(available))
    plan = root_mod.plan_root(model, root["type"], str(request.get("request") or ""))
    refusal = root_mod.refusal(plan, root, names)
    if refusal:
        return refusal
    return pdf.RootPreview(
        key=root_key,
        type_before=root["type"],
        type_after=names[plan.role],
        summary_before=root["summary"],
        summary_after=root_mod.new_summary(model, plan, root["summary"]),
        priority=plan.priority,
        reason=plan.reason,
    )


@tool()
async def notify_email(
    kind: str | None = None,
    issue_key: str | None = None,
    issue_summary: str | None = None,
    project_key: str | None = None,
    situation: str | None = None,
    questions: list[str] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    created_keys: list[str] | None = None,
    errors: list[str] | None = None,
    requirement: str | None = None,
    remaining: list[str] | None = None,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Email the configured recipients about one situation. Never raises.

    ``kind`` is a :class:`NotificationKind` value: ``clarification_required``,
    ``duplicates_found``, ``failed`` or ``created``.
    """
    logger.info("Inside the notify_email tool")
    try:
        parsed = NotificationKind(normalise(kind) or NotificationKind.FAILED.value)
    except ValueError:
        parsed = NotificationKind.FAILED
        logger.warning(f"Unknown notification kind {kind!r}; treating it as 'failed'.")

    base_url, _, _ = jira.jira_creds()
    result = await notifier.send(
        parsed,
        issue_key=normalise(issue_key),
        issue_summary=normalise(issue_summary),
        project_key=normalise(project_key),
        base_url=base_url,
        situation=normalise(situation),
        questions=questions or [],
        duplicates=duplicates or [],
        created_keys=created_keys or [],
        errors=errors or [],
        requirement=normalise(requirement),
        remaining=remaining or [],
        counts=counts or {},
    )
    if result.error:
        logger.error(result.error)
    return result.model_dump(mode="json")


@tool()
async def report_to_issue(
    issue_key: str | None = None,
    headline: str | None = None,
    situation: str | None = None,
    created: list[dict[str, Any]] | None = None,
    questions: list[str] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    errors: list[str] | None = None,
    context_notes: list[str] | None = None,
    conflicts: list[str] | None = None,
    notified: str | None = None,
    mark_processed: bool | None = None,
    mark_awaiting: bool | None = None,
    answered_comment_id: str | None = None,
    quality_warning: str | None = None,
) -> dict[str, Any]:
    """Post the outcome back on the trigger issue and set its marker label.

    The comment is the durable record — an email can be filtered, the ticket
    cannot. The label is what stops the next webhook delivery from starting the
    whole run again.
    """
    logger.info("Inside the report_to_issue tool")

    key = normalise(issue_key).upper()
    if not key:
        return {"ok": False, "comment_id": "", "labels_set": [], "error": "No issue key supplied."}

    base_url, email, token = jira.jira_creds()
    if not (base_url and email and token):
        return {
            "ok": False,
            "comment_id": "",
            "labels_set": [],
            "error": "Missing JIRA_BASE_URL, JIRA_EMAIL, or JIRA_API_TOKEN.",
        }

    refs = [JiraIssueRef.model_validate(item) for item in (created or [])]
    matches = [DuplicateMatch.model_validate(item) for item in (duplicates or [])]

    add: list[str] = []
    remove: list[str] = []
    if mark_processed:
        add.append(jira.processed_label())
        remove.append(jira.awaiting_label())
    if mark_awaiting:
        add.append(jira.awaiting_label())
        remove.append(jira.processed_label())
    # Never ask Jira to add and remove the same label in one call.
    remove = [label for label in remove if label not in add]

    comment_id = ""
    label_error = ""
    try:
        async with jira.jira_client(base_url, email, token) as client:
            body = jira.outcome_comment(
                headline=normalise(headline) or "Run finished.",
                situation=normalise(situation),
                created=refs,
                questions=questions or [],
                duplicates=matches,
                errors=errors or [],
                context_notes=context_notes or [],
                conflicts=conflicts or [],
                notified=normalise(notified),
                quality_warning=normalise(quality_warning),
            )
            comment_id = await jira.add_comment(client, key, body)
            # Record which request this answered, so the same comment is not
            # picked up again by the next delivery.
            await jira.mark_comment_answered(client, key, normalise(answered_comment_id))
            try:
                await jira.set_labels(client, key, add=add, remove=remove)
            except httpx.HTTPError as exc:
                # The comment landed; only the label failed. Worth reporting,
                # not worth failing the run over — but it does mean the next
                # webhook may re-trigger, so say so plainly.
                label_error = (
                    f"Comment posted, but labels could not be updated: {exc}. "
                    f"This issue may be picked up again by the next webhook."
                )
                logger.error(label_error)
    except httpx.HTTPError as exc:
        logger.error(f"Could not report back to {key}: {exc}")
        return {
            "ok": False,
            "comment_id": "",
            "labels_set": [],
            "error": f"Could not comment on {key}: {exc}",
        }

    logger.info(f"Reported outcome on {key} (comment {comment_id}, labels +{add} -{remove})")
    return {
        "ok": not label_error,
        "comment_id": comment_id,
        "labels_set": add,
        "error": label_error,
    }
