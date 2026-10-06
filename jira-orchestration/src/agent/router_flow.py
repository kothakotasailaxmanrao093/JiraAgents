"""The routing decision sequence — pure, SDK-free, and therefore testable.

`router_agent.py` is a thin binding that passes the SDK's executors in. Keeping
the sequence here means the whole router can be tested without Temporal, without
Jira and without a model — which matters, because the properties worth testing
are things like "exactly one comment on every path including failures", and
those are hard to see through a live worker.

**The invariant this module exists to hold: every run either ends ``ignored``
(one of the closed ``IgnoreReason`` set) or attempts exactly one reply.** No
path posts twice, and no failure — in ingress, classification, email or the
post itself — ends a run in silence. Every tool call goes through ``_safe`` so
an exception becomes a result the run can report, never a crash that says
nothing (D0, 2026-09-25).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio.common import RetryPolicy

from routing.catalog import AgentSpec, display_name_for
from routing.classify import Decision, classify, classify_by_verb
from routing.compose import (
    AMBIGUOUS_OPTIONS,
    HELP_OPTIONS,
    compose,
    router_only,
)
from routing.ingress import IngressOutcome
from routing.orchestration import (
    NOUN,
    TRACKED,
    WORKING,
    Next,
    Plan,
    TicketState,
    clock,
    plan_next,
)
from shared.contract import AgentResult, Outcome

logger = logging.getLogger(__name__)

# execute(tool_name, *args, start_to_close_timeout=...) -> dict
Execute = Callable[..., Awaitable[dict]]
# dispatch(agent_name, payload, **options) -> dict  (the child's contract)
Dispatch = Callable[..., Awaitable[dict]]

_INGRESS_TIMEOUT = timedelta(minutes=2)
_NOT_ROUTED = "Not routed — the request could not be read, so it was never classified."
_CLASSIFY_TIMEOUT = timedelta(minutes=2)
_POST_TIMEOUT = timedelta(minutes=2)
# A queued request checks whether the job ahead on its ticket has finished.
_POLL_SECONDS = 10
# sleep(seconds) — asyncio.sleep in the workflow, where it is a durable timer.
Sleep = Callable[[float], Awaitable[None]]


async def run_router(
    payload: dict[str, Any],
    execute: Execute,
    dispatch: Dispatch,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Handle one webhook delivery, start to finish.

    ``sleep`` is how a queued request waits its turn. In the workflow it is
    ``asyncio.sleep``, which Temporal turns into a durable timer.
    """
    issue_key = str(payload.get("issue_key") or "").strip().upper()
    comment_id = str(payload.get("comment_id") or "").strip()
    webhook_event = str(payload.get("webhookEvent") or payload.get("webhook_event") or "")

    # --- gates 1-4, and the run_id everything downstream carries -------------
    gate = await _safe(
        execute,
        "ingress_check",
        issue_key,
        comment_id,
        webhook_event or "comment_created",
        start_to_close_timeout=_INGRESS_TIMEOUT,
    )
    run_id = str(gate.get("run_id") or "")
    outcome = gate.get("outcome")

    if outcome == IngressOutcome.IGNORED.value:
        # Silent by design, and only for the closed IgnoreReason set. Answering
        # "this wasn't for me" on every unrelated comment would make the system
        # unbearable on a busy project.
        logger.info(f"run {run_id}: ignored — {gate.get('ignored')}")
        return {"status": "ignored", "run_id": run_id, "ignored": gate.get("ignored")}

    if outcome != IngressOutcome.PROCEED.value:
        # FAILED, the activity itself raising, or an outcome this build does not
        # know — none of them is "not for me", so the person is told.
        return await _report_unreadable(gate, execute, run_id, issue_key, comment_id)

    body = str(gate.get("comment_body") or "")
    issue_key = str(gate.get("issue_key") or issue_key)
    comment_id = str(gate.get("comment_id") or comment_id)

    # --- classify: Layer 1 free, Layer 2 only if needed ----------------------
    keyword = str(gate.get("trigger_keyword") or "Aetherion")

    # Ask Layer 1 directly rather than inferring from a full classify() result.
    # Inferring meant reading the *reason text* to guess whether a verb had been
    # found, and the no-verb reason contains the word "verb" — so the classifier
    # was never called at all. Decisions must not be recovered from prose.
    decision = classify_by_verb(body, keyword)

    if decision is None:
        # A raised classifier is the same as an unreachable one: scores None,
        # which asks rather than guessing.
        answer = await _safe(
            execute,
            "classify_intent",
            body,
            str(gate.get("issue_summary") or ""),
            start_to_close_timeout=_CLASSIFY_TIMEOUT,
        )
        decision = classify(
            body,
            scores=answer.get("scores"),
            has_children=bool(gate.get("has_children")),
            keyword=keyword,
        )

    logger.info(
        f"run {run_id}: {issue_key} classified {decision.intent} "
        f"(layer {decision.layer}, confidence {decision.confidence:.2f})"
    )

    # --- dispatch, or answer directly ---------------------------------------
    if decision.dispatches:
        assert decision.agent is not None
        # The real task queue, if this deployment needed an override — see
        # AgentSpec.task_queue for why this cannot be read here directly.
        overrides = gate.get("task_queue_overrides") or {}
        agent = decision.agent.with_task_queue(overrides.get(decision.agent.agent_name))
        posted, ran, result = await _orchestrate(
            agent, decision, gate, dispatch, execute, sleep, run_id, issue_key, comment_id
        )
    else:
        blocks, ran, result = _answer_directly(decision)
        # --- ONE reply ------------------------------------------------------
        posted = await _reply(execute, gate, blocks, run_id, issue_key, comment_id)

    return {
        "status": "answered",
        "run_id": run_id,
        "issue_key": issue_key,
        "intent": decision.intent,
        "layer": decision.layer,
        "routed_to": decision.reason,
        "ran": ran,
        "outcome": result.outcome.value if result else None,
        "posted": bool(posted.get("posted")),
        "comment_id": posted.get("comment_id", ""),
        "created": [c.key for c in (result.created if result else [])],
    }


async def _reply(
    execute: Execute,
    gate: dict[str, Any],
    blocks: list[tuple[str, object]],
    run_id: str,
    issue_key: str,
    comment_id: str,
    alert: bool = True,
) -> dict[str, Any]:
    """THE reply to this comment, threaded under it, recorded as answered."""
    return await _post(
        execute,
        issue_key,
        blocks,
        run_id,
        comment_id,
        str(gate.get("idempotency_key") or ""),
        str(gate.get("processed_label") or ""),
        str(gate.get("thread_id") or ""),
        alert,
    )


async def _orchestrate(
    spec: AgentSpec,
    decision: Decision,
    gate: dict[str, Any],
    dispatch: Dispatch,
    execute: Execute,
    sleep: Sleep,
    run_id: str,
    issue_key: str,
    comment_id: str,
) -> tuple[dict[str, Any], list[str], AgentResult | None]:
    """Sequence this request against the ticket's state, answer at once, then finish.

    Still exactly one reply per comment: a job that runs is answered with a
    "processing" reply straight away, and that same comment is edited into the
    result when the job ends (routing/orchestration.py has the rules).
    """
    intent = decision.intent
    fingerprint = str(gate.get("fingerprint") or "")
    min_readiness = int(gate.get("build_min_readiness") or 0)
    state = TicketState.from_dict(gate.get("orchestration"))
    plan = plan_next(intent, state, fingerprint, min_readiness)

    async def reply(blocks: list[tuple[str, object]]) -> dict[str, Any]:
        return await _reply(execute, gate, blocks, run_id, issue_key, comment_id)

    if plan.next is Next.ALREADY_RUNNING:
        return await reply(_already_running(intent, issue_key, state, decision)), [], None
    if plan.next is Next.REUSE_REVIEW:
        previous = AgentResult.from_dict(plan.previous or {})
        reused = (
            f"{spec.display_name} — {decision.reason} The ticket has not changed since the "
            "last review, so that review is shown again; no new review was run."
        )
        return (
            await reply(compose(previous, routed_to=reused, ran=[spec.display_name])),
            [spec.display_name],
            previous,
        )
    if plan.next is Next.BUILD_PAUSED:
        return await reply(_paused(issue_key, plan, decision, min_readiness)), [], None

    # --- a job runs: answer at once, then do it -----------------------------
    queued = plan.next is Next.WAIT
    placeholder = await reply(_working(intent, issue_key, state, decision, queued))
    reply_id = str(placeholder.get("comment_id") or "")
    if not placeholder.get("posted") and not placeholder.get("read_only"):
        # Nowhere to report to (the administrators were just told). Doing the
        # work anyway would build tickets nobody hears about; asking again is
        # safe, because nothing was started.
        return placeholder, [], None

    if queued:
        state = await _wait_for_turn(execute, sleep, issue_key, gate)
        plan = plan_next(intent, state, fingerprint, min_readiness)
        if plan.next is Next.BUILD_PAUSED:
            # The review ahead of this build scored the ticket too low.
            blocks = _paused(issue_key, plan, decision, min_readiness)
            posted = await _finish_reply(
                execute, gate, reply_id, blocks, run_id, issue_key, comment_id
            )
            return posted, [], None
        # Anything else runs now, fresh, with what the job ahead left behind.
        plan = Plan(Next.RUN, plan.review_questions, plan.review_score)
        await _safe(
            execute,
            "update_reply",
            issue_key,
            reply_id,
            _working(intent, issue_key, state, decision, queued=False),
            run_id,
            start_to_close_timeout=_POST_TIMEOUT,
        )

    tracked = intent in TRACKED
    if tracked:
        await _safe(
            execute,
            "claim_ticket",
            issue_key,
            intent,
            run_id,
            reply_id,
            start_to_close_timeout=_POST_TIMEOUT,
        )
    extra = {"review_questions": plan.review_questions} if plan.review_questions else {}
    blocks, ran, result = await _dispatch_and_compose(
        spec, decision, gate, dispatch, execute, run_id, issue_key, comment_id, extra
    )
    if tracked:
        await _safe(
            execute,
            "finish_ticket",
            issue_key,
            intent,
            run_id,
            result.to_dict() if result else None,
            fingerprint,
            start_to_close_timeout=_POST_TIMEOUT,
        )
    posted = await _finish_reply(execute, gate, reply_id, blocks, run_id, issue_key, comment_id)
    return posted, ran, result


async def _wait_for_turn(
    execute: Execute, sleep: Sleep, issue_key: str, gate: dict[str, Any]
) -> TicketState:
    """Wait until the job ahead on this ticket ends (or the wait runs out)."""
    rounds = max(1, int(gate.get("queue_wait_minutes") or 10) * 60 // _POLL_SECONDS)
    state = TicketState()
    for _ in range(rounds):
        await sleep(_POLL_SECONDS)
        read = await _safe(execute, "ticket_state", issue_key, start_to_close_timeout=_POST_TIMEOUT)
        state = TicketState.from_dict(read.get("state"))
        if state.active is None:
            return state
    logger.warning(f"{issue_key}: the job ahead did not finish in time; running anyway")
    return TicketState(last=state.last)


async def _finish_reply(
    execute: Execute,
    gate: dict[str, Any],
    reply_id: str,
    blocks: list[tuple[str, object]],
    run_id: str,
    issue_key: str,
    comment_id: str,
) -> dict[str, Any]:
    """Turn the "processing" reply into the result — or post it, if it cannot be edited."""
    # The "processing" reply never posted, and the administrators were told
    # then; a second failure here is the same problem, not a new one.
    alert = bool(reply_id)
    if reply_id:
        edited = await _safe(
            execute,
            "update_reply",
            issue_key,
            reply_id,
            blocks,
            run_id,
            start_to_close_timeout=_POST_TIMEOUT,
        )
        if edited.get("updated"):
            return {"posted": True, "comment_id": reply_id, "updated": True}
    return await _reply(execute, gate, blocks, run_id, issue_key, comment_id, alert)


def _working(
    intent: str, issue_key: str, state: TicketState, decision: Decision, queued: bool
) -> list[tuple[str, object]]:
    """The reply posted the moment a job is accepted."""
    doing = WORKING.get(intent, "Working on")
    if queued and state.active is not None:
        ahead = state.active
        return router_only(
            f"⏳ {NOUN.get(intent, 'request').capitalize()} queued for {issue_key}",
            routed_to=decision.reason,
            what_happened=(
                f"Waiting for the {NOUN.get(ahead.intent, 'job')} of {issue_key} that started "
                f"at {clock(ahead.started)} to finish. This {NOUN.get(intent, 'request')} will "
                f"then start by itself, on {issue_key}, and its result will replace this message."
            ),
        )
    return router_only(
        f"⏳ {doing} {issue_key}…",
        routed_to=decision.reason,
        what_happened=(
            "Started. The result will replace this message — usually within "
            f"{'4' if intent == 'BUILD' else '2'} minutes."
        ),
    )


def _already_running(
    intent: str, issue_key: str, state: TicketState, decision: Decision
) -> list[tuple[str, object]]:
    started = clock(state.active.started) if state.active else "earlier"
    return router_only(
        f"⏳ Already {WORKING.get(intent, 'working on').lower()} {issue_key}",
        routed_to=decision.reason,
        what_happened=(
            f"A {NOUN.get(intent, 'request')} of {issue_key} started at {started} is still "
            "running. Its result will appear in that reply — nothing new was started."
        ),
    )


def _paused(
    issue_key: str, plan: Plan, decision: Decision, min_readiness: int
) -> list[tuple[str, object]]:
    return router_only(
        f"Build paused — the review scored {issue_key} {plan.review_score}/5",
        routed_to=decision.reason,
        what_happened=(
            f"The last review of {issue_key} scored it {plan.review_score}/5, below the "
            f"{min_readiness}/5 needed to build, and the ticket has not changed since. "
            "Nothing was created. Answer these in the description (or attach them), then "
            "ask again — an edited ticket builds straight away:"
        ),
        options=plan.review_questions or ["Add the missing details the review listed."],
    )


async def _safe(execute: Execute, name: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run one tool; an exception becomes ``{"raised": …}``, never a crash.

    A crashed workflow posts nothing and emails nobody — the silence D0 was
    about. Every caller decides what a raised result means for the reply.
    """
    try:
        result = await execute(name, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — reported by the caller, never dropped
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.error(f"{name} raised: {detail}")
        return {"raised": detail, "error": detail}
    return result if isinstance(result, dict) else {}


async def _post(
    execute: Execute,
    issue_key: str,
    blocks: list[tuple[str, object]],
    run_id: str,
    comment_id: str,
    idempotency: str = "",
    label: str = "",
    thread_id: str = "",
    alert: bool = True,
) -> dict[str, Any]:
    """Post THE reply; if it cannot be posted, tell the administrators.

    ``alert=False`` when they were already told about this reply — the
    "processing" message failed the same way a moment earlier.

    The one failure the user cannot see, because the channel for telling them
    is the thing that broke.
    """
    posted = await _safe(
        execute,
        "post_reply",
        issue_key,
        blocks,
        run_id,
        comment_id,
        idempotency,
        label,
        thread_id,
        start_to_close_timeout=_POST_TIMEOUT,
    )
    if alert and not posted.get("posted") and not posted.get("read_only"):
        await _safe(
            execute,
            "notify_admins",
            f"Could not post the reply on {issue_key}",
            str(posted.get("error") or "unknown error"),
            run_id,
            start_to_close_timeout=_POST_TIMEOUT,
        )
    return posted


async def _report_unreadable(
    gate: dict[str, Any],
    execute: Execute,
    run_id: str,
    issue_key: str,
    comment_id: str,
) -> dict[str, Any]:
    """Ingress failed: say so on the ticket, and alert the administrators.

    A permissions failure at ingress will usually block this reply too — it is
    attempted anyway, because a transient 5xx or one failing sub-call leaves
    commenting possible, and ``_post`` alerts the administrators if it is not.
    """
    issue_key = str(gate.get("issue_key") or issue_key)
    comment_id = str(gate.get("comment_id") or comment_id)
    problem = str(gate.get("problem") or f"Could not read {issue_key}: {gate.get('error')}")
    logger.error(f"run {run_id}: could not read {issue_key} — {gate.get('error')}")
    notified = await _safe(
        execute,
        "notify_admins",
        f"Could not read {issue_key}",
        problem,
        run_id,
        start_to_close_timeout=_POST_TIMEOUT,
    )
    blocks = router_only(
        "Could not read this ticket — nothing was done",
        routed_to=_NOT_ROUTED,
        what_happened=(
            f"I received your request but could not read {issue_key}. Nothing was "
            "created and nothing was changed, so asking again is safe once this is "
            "resolved."
        ),
        problems=[problem],
        notification=_admin_alert_line(notified),
    )
    # Threaded under the asking comment; post_reply falls back to a plain
    # comment if Jira refuses. No label and no answered record: this request
    # was never handled, and asking again must work.
    posted = await _post(execute, issue_key, blocks, run_id, comment_id, "", "", comment_id)
    return {
        "status": "failed",
        "run_id": run_id,
        "issue_key": issue_key,
        "error": gate.get("error", ""),
        "posted": bool(posted.get("posted")),
    }


async def _dispatch_and_compose(
    spec: AgentSpec,
    decision: Decision,
    gate: dict[str, Any],
    dispatch: Dispatch,
    execute: Execute,
    run_id: str,
    issue_key: str,
    comment_id: str,
    extra_payload: dict[str, Any] | None = None,
) -> tuple[list[tuple[str, object]], list[str], AgentResult | None]:
    """Call one child and turn its answer into the reply.

    A child that fails still produces a reply — the router owns the comment, so a
    dead child cannot mean silence.
    """
    child_payload: dict[str, Any] = {
        "issue_key": issue_key,
        "comment_id": comment_id,
        "run_id": run_id,
        # What the orchestrator knows that the child should use (a previous
        # review's open questions, for a build).
        **(extra_payload or {}),
    }
    # Forced last, so no caller and no configuration can override them.
    child_payload.update(spec.forced_payload)

    options: dict[str, Any] = {
        "execution_timeout": timedelta(minutes=spec.timeout_minutes),
        "workflow_id": f"{spec.intent.lower()}-{run_id}",
        # Bounds Temporal's retry of a child that STARTS and then fails — e.g.
        # a transient exception inside the child's own run. It does NOT bound
        # a child that never starts at all because no worker ever polls its
        # task queue (the F22 misconfigured-queue case): that sits "Running"
        # with no failure to retry, so this policy plays no part, and
        # `execution_timeout` above is the only thing that ends it. An earlier
        # version of this comment claimed this policy alone made "every
        # dispatch fail fast" — a live run through the exact never-started
        # case disproved that, taking the full execution_timeout instead of
        # the ~14s this policy budgets. Left honest rather than restated
        # confidently: RetryPolicy.non_retryable_error_types could still
        # distinguish "wrong name, will never succeed" from "worker
        # restarting, try again in a moment" for the case this DOES cover,
        # but the platform's real error-type strings for that are still
        # unconfirmed.
        "retry_policy": RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=10),
            maximum_attempts=3,
        ),
    }
    if spec.task_queue:
        # Without this, Temporal defaults a child workflow to the CALLER's
        # queue when none is given — the router's own — and the child fails
        # immediately because that worker never registered its workflow type.
        options["task_queue"] = spec.task_queue

    routed_to_ran = f"{spec.display_name} — {decision.reason}"
    routed_to_failed = (
        f"{spec.display_name} — {decision.reason.rstrip('.')} — "
        "but that agent could not be reached."
    )

    try:
        raw = await dispatch(spec.agent_name, child_payload, **options)
    except Exception as exc:  # noqa: BLE001 — every failure still gets a reply
        # The real cause, never flattened: logged with the run_id for tracing,
        # AND put in the reply's Problems section so the person reading the
        # ticket is not left with only "did not respond".
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.error(f"run {run_id}: dispatch to {spec.agent_name} failed: {detail}")

        notified = await _safe(
            execute,
            "notify_admins",
            f"{spec.display_name} did not respond on {issue_key}",
            detail,
            run_id,
            start_to_close_timeout=_POST_TIMEOUT,
        )
        notification = _admin_alert_line(notified)

        return (
            router_only(
                "Could not complete this request",
                routed_to=routed_to_failed,
                what_happened=(
                    f"The {spec.display_name} agent did not respond. "
                    + (
                        "Nothing was created, so asking again is safe."
                        if not spec.writes_to_jira
                        else "It may not have finished, so check the ticket before asking again."
                    )
                ),
                problems=[f"Dispatch to the {spec.display_name} agent failed: {detail}"],
                notification=notification,
            ),
            [],  # nothing ran to completion, so nothing is named
            None,
        )

    try:
        result = AgentResult.from_dict(raw if isinstance(raw, dict) else (raw or [{}])[0])
    except Exception as exc:  # noqa: BLE001 — a malformed contract is still reportable
        detail = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.error(f"run {run_id}: {spec.agent_name} returned an unusable result: {detail}")
        return (
            router_only(
                "Could not complete this request",
                routed_to=routed_to_ran,
                what_happened=f"The {spec.display_name} agent returned something unreadable.",
                problems=[detail],
            ),
            [],
            None,
        )

    # The router decides and sends the one outcome email (the children stand
    # down in delegated mode). Sent before the reply is composed, so the reply
    # can say what really happened rather than what was intended.
    notification = ""
    kind = _EMAIL_KIND_FOR.get(result.outcome)
    if kind:
        emailed = await _safe(
            execute,
            "send_outcome_email",
            kind,
            issue_key,
            result.headline,
            _email_details(result),
            run_id,
            start_to_close_timeout=_POST_TIMEOUT,
        )
        notification = _outcome_email_line(emailed)
    blocks = compose(
        result,
        routed_to=routed_to_ran,
        ran=[display_name_for(result.produced_by) or spec.display_name],
        notification=notification,
    )
    return blocks, [spec.display_name], result


# Outcome -> the email kind, by the names ORCH_NOTIFY_ON and LTW_NOTIFY_ON use.
# NOT_A_REQUIREMENT and REVIEWED are absent on purpose: an invalid request never
# emails, and a review writes nothing a person has to act on.
_EMAIL_KIND_FOR = {
    Outcome.CREATED: "created",
    Outcome.NEEDS_INFO: "clarification",
    Outcome.ALREADY_EXISTS: "duplicates",
    Outcome.FAILED: "failed",
}


def _email_details(result: AgentResult) -> list[str]:
    """The facts a person needs from the email, without opening Jira."""
    lines = [result.summary] if result.summary else []
    lines += [f"Created {c.issue_type} {c.key}: {c.summary}".strip() for c in result.created]
    lines += [f"Question: {q}" for q in result.questions]
    lines += [f"Already exists: {d.existing_key} {d.existing_summary}" for d in result.duplicates]
    lines += [f"Problem: {e}" for e in result.errors]
    return lines


def _outcome_email_line(emailed: dict[str, Any]) -> str:
    """Only what happened. Silent when it was not wanted (ORCH_NOTIFY_ON)."""
    if emailed.get("sent"):
        return f"Emailed {', '.join(emailed.get('recipients') or [])}."
    if emailed.get("suppressed_repeat"):
        return "Not emailed again — the same email was sent a few minutes ago."
    if emailed.get("error"):
        return f"No email was sent — {emailed['error']}"
    return ""


def _admin_alert_line(notified: dict[str, Any]) -> str:
    """What the reply may say about the admin alert — only what happened.

    Keyed on ``sent``, never ``attempted``: ``notify_admins`` attempts and logs
    but has no sender, so "Emailed the administrators" was false every time.
    """
    if notified.get("sent"):
        return "Emailed the administrators."
    if notified.get("attempted"):
        return "Could not email the administrators — this was logged instead."
    return ""


def _answer_directly(
    decision: Decision,
) -> tuple[list[tuple[str, object]], list[str], None]:
    """The router's own replies: help, questions, chatter, ambiguity.

    No child ran, so "Handled by" names none — the router knows what it *chose*,
    and must not claim anything *ran*. No run_id parameter — see
    compose.handled_by for why it never reaches a rendered comment.
    """
    if decision.intent == "HELP":
        return (
            router_only(
                "What I can do",
                routed_to=decision.reason,
                options=HELP_OPTIONS,
            ),
            [],
            None,
        )

    if decision.intent == "CHATTER":
        return (
            router_only(
                "Invalid request — this is not a work requirement",
                routed_to=decision.reason,
                what_happened=(
                    "Mention the agent again with either a description of what should "
                    "be built, or a request to review this ticket."
                ),
            ),
            [],
            None,
        )

    if decision.intent == "QUESTION":
        # Reached only if the catalog has no BUILD-intent agent configured —
        # classify.py always sets ``agent=by_intent("BUILD")`` for QUESTION
        # (Bug 5), so under any valid catalog this decision dispatches instead
        # of landing here. Kept as the fallback for a misconfigured catalog
        # rather than letting the router crash on ``spec.display_name``.
        return (
            router_only(
                "About this ticket",
                routed_to=decision.reason,
                what_happened=(
                    "I can break this ticket into Jira issues, or review it for gaps. "
                    "For anything else about it, the ticket's own description and "
                    "comments are the best source."
                ),
                options=HELP_OPTIONS,
            ),
            [],
            None,
        )

    # AMBIGUOUS — Layer 3. One round-trip is far cheaper than ten wrongly
    # created stories.
    return (
        router_only(
            "Which did you mean?",
            routed_to=decision.reason,
            what_happened="I can do either of these — reply mentioning the agent again with:",
            options=AMBIGUOUS_OPTIONS,
        ),
        [],
        None,
    )


# --------------------------------------------------------------------------
# Health check — "is each configured child actually reachable?"
# --------------------------------------------------------------------------
#
# agentExecutor.execute starts a Temporal CHILD WORKFLOW, and starting a child
# workflow is a workflow-context-only operation in Temporal's own architecture
# — it cannot be done from an activity/tool. So this cannot be a separate
# @tool the way ingress_check or post_reply are; it has to run inside the
# SAME running JiraOrchestration workflow, reusing the identical `dispatch`
# callable that answers real comments. That is deliberate: a health check that
# used a different code path could pass while the real path still fails.
#
# There is no "at startup" hook Temporal workflows have — a workflow only runs
# when something invokes it. The practical equivalent is "run this on demand,
# immediately after every publish" — see PUBLISH.md — rather than a hook that
# does not exist on this platform.


async def run_health_check(dispatch: Dispatch, execute: Execute) -> dict[str, Any]:
    """Dispatch a minimal, side-effect-free payload to every catalog agent, and
    check the Gmail login the outcome and admin emails depend on.

    Invoke directly:

        uv run aetherion agent JiraOrchestration '{"health_check": true}'

    A misconfigured task queue or an agent that was never published shows up
    here — this is what would have caught F22 before a user typed
    "@Aetherion review" and got nothing.
    """
    from routing.catalog import CATALOG

    results: dict[str, Any] = {}
    for spec in CATALOG:
        # issue_key "FL" alone is a real, already-observed safe no-op: both
        # children read it as "not a real issue key, nothing to do" and return
        # immediately without creating or reading anything. This is not a
        # fabricated ping payload — it is the same shape a malformed webhook
        # event already produces in production.
        probe_payload: dict[str, Any] = {
            "issue_key": "FL",
            "comment_id": "healthcheck",
            "run_id": "healthcheck",
        }
        probe_payload.update(spec.forced_payload)

        options: dict[str, Any] = {
            "execution_timeout": timedelta(seconds=30),
            "workflow_id": f"healthcheck-{spec.intent.lower()}",
            "retry_policy": RetryPolicy(maximum_attempts=1),
        }
        if spec.task_queue:
            options["task_queue"] = spec.task_queue

        try:
            await dispatch(spec.agent_name, probe_payload, **options)
            results[spec.agent_name] = {"reachable": True}
        except Exception as exc:  # noqa: BLE001 — the point is to report, not raise
            results[spec.agent_name] = {
                "reachable": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }

    # An app password dies silently (Google revokes it when 2-Step Verification
    # is reset); a failed login here is a missing email caught in advance.
    smtp = await _safe(execute, "smtp_login_check", start_to_close_timeout=_POST_TIMEOUT)
    return {
        "status": "health_check",
        "agents": results,
        "smtp": {"ok": bool(smtp.get("ok")), "error": str(smtp.get("error") or "")},
    }


# --- GitHub → Planning (2026-10-03) -----------------------------------------------

_GITHUB_SCAN_TIMEOUT = timedelta(minutes=3)
POLL_RUN_ID = "github-poll"


async def run_github_poll(execute: Execute, dispatch: Dispatch) -> dict[str, Any]:
    """One check of GitHub: every new merge into the main branch, and every new
    batch of PR comments, goes to the Planning agent as its ``issue_text``.

    Started every minute (Aetherion Schedule, or scripts/run_local.sh --poll):

        {"github_poll": true}

    An event is recorded as sent only after Planning reports success; a failed
    run is retried at the next check, and the admins are told when the retries
    run out. Never raises: the result says what happened.
    """
    scan = await _safe(execute, "github_scan", start_to_close_timeout=_GITHUB_SCAN_TIMEOUT)
    if not scan.get("ok"):
        return {"status": "github_poll", "error": scan.get("error") or "scan failed", "sent": 0}

    results: list[dict[str, Any]] = []
    for event in scan.get("events") or []:
        ok, message = await _give_to_planning(dispatch, scan, event)
        done = await _safe(
            execute,
            "github_event_done",
            event["key"],
            ok,
            message,
            start_to_close_timeout=_POST_TIMEOUT,
        )
        if done.get("gave_up"):
            await _safe(
                execute,
                "notify_admins",
                f"PR #{event['pr_number']} in {event['repo']} could not be given to Planning",
                f"{event['kind']} — {event['pr_url']}\nLast error: {message}\n"
                "It will not be retried automatically.",
                POLL_RUN_ID,
                start_to_close_timeout=_POST_TIMEOUT,
            )
        results.append({"event": event["key"], "ok": ok, "message": message})

    return {
        "status": "github_poll",
        "repos": scan.get("repos", 0),
        "sent": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
        "notes": scan.get("notes") or [],
    }


async def _give_to_planning(
    dispatch: Dispatch, scan: dict[str, Any], event: dict[str, Any]
) -> tuple[bool, str]:
    """Start the Planning agent with the event as its input; (succeeded, message)."""
    payload = {
        "issue_text": event["issue_text"],
        # Not read by Planning today; there for it to use.
        "event": event["kind"],
        "repo": event["repo"],
        "pr_number": event["pr_number"],
        "pr_url": event["pr_url"],
        "issue_key": event.get("issue_key") or "",
    }
    options: dict[str, Any] = {
        "workflow_id": event["workflow_id"],
        "execution_timeout": timedelta(minutes=int(scan.get("timeout_minutes") or 15)),
        "retry_policy": RetryPolicy(maximum_attempts=1),  # retried by the next check
    }
    if scan.get("planning_queue"):
        options["task_queue"] = scan["planning_queue"]
    try:
        out = await dispatch(scan["planning_agent"], payload, **options)
    except Exception as exc:  # noqa: BLE001 — recorded and retried, never raised
        return False, f"{type(exc).__name__}: {str(exc)[:300]}"
    out = out if isinstance(out, dict) else {}
    if out.get("status") == "success":
        return True, "plan generated"
    return False, str(out.get("message") or out.get("error") or "Planning returned no plan")[:300]
