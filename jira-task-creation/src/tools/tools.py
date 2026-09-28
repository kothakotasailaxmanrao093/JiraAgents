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

import os
from typing import Any

import httpx
from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from pydantic import ValidationError

from src.classification.decompose import (
    DecompositionError,
    build_breakdown,
    explain_issue,
    llm_same_work,
    strip_section_headings,
    triage,
)
from src.classification.request import RequestBucket, classify_request
from src.classification.validate import ProjectContext, normalise, validate_input
from src.confluence import client as confluence_mod
from src.context import attachments as attachments_mod
from src.context.ingest import (
    assemble_requirement,
    describe_issue,
    detect_placement,
    find_conflicts,
    find_unread_links,
    is_pointer_comment,
    is_question_comment,
    request_is_only_a_pointer,
    requirement_core,
    strip_trigger_mentions,
)
from src.jira import api as jira
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
    SourceIssue,
    SprintPlacement,
    WorkBreakdown,
    allow_source_files_from,
)
from src.notifications import email as notifier

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
            # --- the project itself ---------------------------------------
            try:
                project = await jira.fetch_project(client, key)
            except httpx.HTTPStatusError as exc:
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

            try:
                project.issue_types = await jira.available_issue_types(client, key)
            except httpx.HTTPError as exc:
                return _context_failure(f"Could not list issue types for '{key}': {exc}")

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
            try:
                existing, unavailable = await jira.fetch_existing_issues(
                    client, key, base_url=base_url
                )
                notes.append(f"Compared against {len(existing)} existing ticket(s) in {key}.")
            except httpx.HTTPError as exc:
                existing = []
                unavailable.append(f"Could not read existing tickets for context: {exc}")

            # --- an existing epic, when one was supplied ------------------
            epic_ctx = None
            if epic_key:
                try:
                    epic_ctx = await jira.fetch_epic(client, epic_key, key)
                    notes.append(
                        f"New stories will be added under existing epic {epic_ctx.key} "
                        f"({len(epic_ctx.child_stories)} story/stories already on it)."
                    )
                except jira.EpicValidationError as exc:
                    return _context_failure(str(exc))
                except httpx.HTTPError as exc:
                    return _context_failure(f"Could not read epic '{epic_key}': {exc}")

            # --- the active sprint, when sprint placement was chosen ------
            sprint = None
            if want is SprintPlacement.CURRENT_SPRINT:
                try:
                    sprint = await jira.active_sprint(client, key)
                    notes.append(
                        f"New stories will be added to active sprint "
                        f"'{sprint.name}' (id {sprint.id})."
                    )
                except jira.SprintUnavailable as exc:
                    # The requester asked for "the current sprint" and Jira
                    # cannot say which one that is. That is a question for them,
                    # not a misconfiguration, so it is raised as a clarification.
                    return _context_failure(
                        f"Current sprint was requested but is unavailable: {exc}",
                        notes,
                        needs_clarification=True,
                    )
                except httpx.HTTPError as exc:
                    return _context_failure(
                        f"Could not resolve the active sprint for '{key}': {exc}", notes
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
    existing = [i for i in existing if own_label not in i.labels]

    # Only Story titles are compared. The Epic summary restates the whole
    # requirement, so including it made one long string collide with everything.
    proposed = [story.title for story in breakdown.stories]
    base_url, _, _ = jira.jira_creds()
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

    # --- description matches need the model's agreement -------------------
    # Word containment in a long description is weak evidence, and it blocks
    # work outright. BGV-57's "Officer ID Verification" was "covered" by a
    # ticket about address history. So a description match stands only if the
    # model agrees it is the same work; if the model cannot be asked, the word
    # match stands, as it always did.
    by_description = [m for m in overlaps if m.matched_on == "description"]
    if by_description and jira.adjudication_enabled():
        pairs = [(m.proposed_title, m.existing_key, m.existing_summary) for m in by_description]
        try:
            agreed = await llm_same_work(pairs)
        except Exception as exc:  # noqa: BLE001 — keep the word match
            logger.info(f"Description-match adjudication unavailable: {exc}")
            agreed = set(range(len(by_description)))
        rejected = [m for i, m in enumerate(by_description) if i not in agreed]
        if rejected:
            logger.info(
                f"Model rejected {len(rejected)} description match(es): "
                + "; ".join(f"{m.proposed_title} vs {m.existing_key}" for m in rejected)
            )
            overlaps = [m for m in overlaps if m not in rejected]
            covered = {m.proposed_title for m in overlaps}
            report = OverlapReport(
                matches=overlaps,
                covered_titles=[t for t in proposed if t in covered],
                remaining_titles=[t for t in proposed if t not in covered],
            )

    # --- near misses: the band word overlap cannot decide -----------------
    # Jaccard scores "Let drivers record a rest break" against "Allow drivers
    # to log a rest break" at 0.60 — the same work, below the 0.75 line, so a
    # full duplicate hierarchy used to be created. Lowering the threshold is
    # not the answer: "Send email notifications" and "Send SMS notifications"
    # also score 0.60 and are genuinely different work. Only meaning separates
    # them, so anything in the band gets a second opinion from the model.
    #
    # Failure is silent on purpose: no model, no confirmation, no match — which
    # is exactly what happened before this existed.
    adjudicated: list[DuplicateMatch] = []
    if existing and jira.adjudication_enabled():
        uncovered = set(report.remaining_titles)
        near = [
            match
            for match in jira.find_near_misses(
                proposed, existing, base_url=base_url, exclude_keys=exclude
            )
            if match.proposed_title in uncovered
        ]
        if near:
            pairs = [(m.proposed_title, m.existing_key, m.existing_summary) for m in near]
            try:
                confirmed = await llm_same_work(pairs)
            except Exception as exc:
                logger.info(f"Near-miss adjudication unavailable, keeping word scores: {exc}")
                confirmed = {}
            adjudicated = [
                near[i].model_copy(update={"matched_on": "semantic"}) for i in sorted(confirmed)
            ]
            if adjudicated:
                logger.info(
                    f"{len(adjudicated)} of {len(near)} near miss(es) confirmed as "
                    f"existing work by the model"
                )
                overlaps = overlaps + adjudicated
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


@tool()
async def create_jira_issues(
    breakdown: dict[str, Any] | None = None,
    requirement: str | None = None,
    project_key: str | None = None,
    correlation_id: str | None = None,
    extra_labels: list[str] | None = None,
    existing_epic_key: str | None = None,
    sprint_id: int | None = None,
    attach_to_key: str | None = None,
    idempotency_basis: str | None = None,
    source_key: str | None = None,
) -> dict[str, Any]:
    """Create the Jira hierarchy for an already-validated breakdown.

    Refuses before any write when the breakdown fails schema validation, the
    Jira configuration is incomplete, the project key is invalid, or a required
    issue type is unavailable in that project. Re-running with the same
    requirement finds the previous issues by idempotency label and returns them
    rather than creating duplicates.
    """
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
    key_label = jira.idempotency_key(basis, key, normalise(correlation_id))
    logger.info(f"idempotency_key : {key_label} (basis {basis[:80]!r})")

    try:
        async with jira.jira_client(base_url, email, token) as client:
            # --- Preflight: project reachable, issue types present, no duplicate.
            try:
                available = await jira.available_issue_types(client, key)
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

            result = await jira.create_hierarchy(
                client,
                base_url,
                key,
                model,
                key_label,
                extra_labels,
                existing_epic_key=normalise(existing_epic_key).upper(),
                attach_to_key=normalise(attach_to_key).upper(),
                types=types,
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
            # on. Sub-tasks attached to that ticket are already its children.
            source = normalise(source_key).upper()
            if source and result.status is ResultStatus.JIRA_CREATED and not result.attached_to:
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
                payload = await jira.fetch_issue(client, key)
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
            answered = await jira.answered_comment_ids(client, key) if skip_processed else set()
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

            # --- attachments ----------------------------------------------
            attachments: list[AttachmentText] = []
            raw_attachments = fields.get("attachment") or []
            if raw_attachments and matched and not processed:
                limit = attachments_mod.max_files()
                if len(raw_attachments) > limit:
                    notes.append(
                        f"{len(raw_attachments)} attachments found; only the first "
                        f"{limit} were read."
                    )
                for item in raw_attachments[:limit]:
                    filename = item.get("filename", "") or ""
                    mime = item.get("mimeType", "") or ""
                    url = item.get("content", "") or ""
                    if not (filename and url):
                        continue
                    if not attachments_mod.supported(filename):
                        attachments.append(await attachments_mod.extract(b"", filename, mime))
                        continue
                    try:
                        data = await jira.download_attachment(client, url)
                    except httpx.HTTPError as exc:
                        attachments.append(
                            AttachmentText(
                                filename=filename,
                                mime_type=mime,
                                note=f"Could not download this attachment: {exc}",
                            )
                        )
                        continue
                    attachments.append(await attachments_mod.extract(data, filename, mime))

            # --- linked Confluence pages ----------------------------------
            # A team that writes its spec in Confluence and links it was, until
            # now, handing the agent a URL and nothing else.
            confluence_pages: list[ConfluencePage] = []
            if matched and not processed and confluence_mod.enabled():
                # A linked page is supporting material, exactly like an
                # attachment: worth reading, never worth failing the run over.
                try:
                    remote_urls = await confluence_mod.fetch_remote_link_urls(client, key)
                    scan = [trigger.body if trigger else "", description, summary]
                    confluence_pages, page_notes = await confluence_mod.fetch_linked_pages(
                        client, scan, base_url, remote_urls
                    )
                    notes.extend(page_notes)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(f"Confluence lookup failed for {key}: {exc}")
                    notes.append(f"Linked Confluence pages could not be read: {exc}")
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
        trigger_comment_body=trigger.body if trigger else "",
        trigger_is_pointer=bool(trigger and is_pointer_comment(trigger.body)),
    )

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
