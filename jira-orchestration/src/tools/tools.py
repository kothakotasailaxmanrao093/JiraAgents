"""The router's activities — everything the workflow may not do itself.

A Temporal workflow must be deterministic: no environment, no files, no network,
no clock, no randomness. So every one of those lives here, behind a `@tool`, and
the workflow receives only the answer.

What is here and why it has to be:

| Activity | Why it cannot be workflow code |
|---|---|
| `ingress_check` | reads the environment, calls Jira, mints a uuid |
| `classify_intent` | calls a model over the network |
| `post_reply` | writes to Jira |
| `notify_admins` | sends email |
| `send_outcome_email` | sends email |
| `smtp_login_check` | logs in to Gmail (health check; sends nothing) |

The split is not bureaucratic. `RestrictedWorkflowAccessError` has taken this
system's sibling down in production, and the stack trace was three frames deep
into a helper nobody thought of as configuration.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from aetherion_sdk import tool
from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv

from routing import settings
from routing.ingress import IgnoreReason, IngressOutcome, idempotency_key, screen_comment
from shared.adf import blocks_to_doc, comment_text
from shared.mailer import Delivery, check_login, deliver

load_dotenv()
logger = setup_logger(__name__)

_TIMEOUT = 30.0
# Fields the router needs, and no more. It routes; it does not gather context.
_ISSUE_FIELDS = "summary,labels,project,subtasks,comment"


def _client() -> httpx.AsyncClient:
    base, email, token = settings.jira_credentials()
    if not (base and email and token):
        raise ValueError(
            "Missing Jira configuration. Set JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN."
        )
    return httpx.AsyncClient(
        base_url=base,
        auth=(email, token),
        timeout=_TIMEOUT,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )


async def _answered_ids(client: httpx.AsyncClient, issue_key: str) -> set[str]:
    """Comment ids this router has already answered on an issue.

    Kept in a Jira issue property so the same ticket can take many requests over
    its life, and so a restart does not make it answer everything again.
    """
    try:
        resp = await client.get(
            f"/rest/api/3/issue/{issue_key}/properties/{settings.ANSWERED_PROPERTY}"
        )
        if resp.status_code == 404:
            return set()
        resp.raise_for_status()
        return {str(x) for x in (resp.json().get("value") or {}).get("ids") or []}
    except httpx.HTTPError as exc:
        # Better to risk answering twice than to refuse every request.
        logger.warning(f"Could not read answered comments for {issue_key}: {exc}")
        return set()


async def _thread_root(client: httpx.AsyncClient, issue_key: str, comment_id: str) -> str:
    """The comment a reply should be threaded under.

    Normally the triggering comment itself. Jira threads are one level deep and
    refuses a reply to a reply, so a trigger that is itself a reply is answered
    under its parent. ``parentId`` appears only on the single-comment read, not
    in the issue's comment field, hence the extra call.
    """
    try:
        resp = await client.get(f"/rest/api/3/issue/{issue_key}/comment/{comment_id}")
        resp.raise_for_status()
        return str(resp.json().get("parentId") or comment_id)
    except httpx.HTTPError as exc:
        # post_reply falls back to a plain comment if this guess is refused.
        logger.warning(f"Could not read the thread of comment {comment_id}: {exc}")
        return comment_id


@tool(name="ingress_check")
async def ingress_check(
    issue_key: str,
    comment_id: str,
    webhook_event: str = "comment_created",
) -> dict[str, Any]:
    """Gates 1-4, plus everything the workflow needs to decide with.

    Every return carries ``outcome`` — ``proceed``, ``ignored`` (with one
    ``IgnoreReason``) or ``failed`` (with ``problem``, a sentence for the
    reply). The workflow stays silent only for ``ignored``.

    The ``run_id`` is minted here because ``uuid4()`` is non-deterministic. It is
    the correlation id for everything downstream: the Jira comment, the email
    and every log line carry it.
    """
    issue_key = (issue_key or "").strip().upper()
    comment_id = str(comment_id or "").strip()
    run_id = uuid.uuid4().hex[:8]

    if not issue_key:
        return _ignored(IgnoreReason.NO_ISSUE, run_id)

    allowed = settings.allowed_project_keys()
    if allowed and issue_key.split("-")[0] not in allowed:
        return _ignored(IgnoreReason.OUTSIDE_PROJECT, run_id)

    try:
        async with _client() as client:
            resp = await client.get(
                f"/rest/api/3/issue/{issue_key}", params={"fields": _ISSUE_FIELDS}
            )
            resp.raise_for_status()
            issue = resp.json()

            fields = issue.get("fields") or {}
            comments = ((fields.get("comment") or {}).get("comments")) or []

            target = None
            for candidate in comments:
                if str(candidate.get("id") or "") == comment_id:
                    target = candidate
                    break
            if target is None and comments:
                target = comments[-1]
                comment_id = str(target.get("id") or "")
            if target is None:
                return _ignored(IgnoreReason.NO_COMMENT, run_id)

            body = comment_text(target.get("body"), keep=settings.trigger_keyword())
            author = (target.get("author") or {}).get("accountId") or ""

            verdict = screen_comment(
                body,
                keyword=settings.trigger_keyword(),
                author_account_id=str(author),
                bot_account_id=settings.bot_account_id(),
            )
            if verdict.ignored is not None:
                return _ignored(verdict.ignored, run_id)

            # Gate 3. Checked BEFORE classification so a redelivery costs neither
            # a model call nor a child workflow.
            answered = await _answered_ids(client, issue_key)
            key = idempotency_key(comment_id, webhook_event)
            if key in answered or comment_id in answered:
                logger.info(f"run {run_id}: duplicate delivery for comment {comment_id}, ignored")
                return _ignored(IgnoreReason.DUPLICATE, run_id)

            return {
                "outcome": IngressOutcome.PROCEED.value,
                "run_id": run_id,
                "issue_key": issue_key,
                "comment_id": comment_id,
                "comment_body": body,
                "thread_id": await _thread_root(client, issue_key, comment_id),
                "issue_summary": fields.get("summary") or "",
                # A signal the classifier uses: an issue that already has
                # sub-tasks has almost certainly been broken down already.
                "has_children": bool(fields.get("subtasks")),
                "idempotency_key": key,
                "trigger_keyword": settings.trigger_keyword(),
                "processed_label": settings.processed_label(),
                "existing_labels": list(fields.get("labels") or []),
                "read_only": settings.read_only(),
                "task_queue_overrides": settings.task_queue_overrides(),
            }
    except Exception as exc:  # noqa: BLE001 — a failure is reported, never dropped
        logger.error(f"run {run_id}: ingress failed on {issue_key}: {exc}", exc_info=True)
        return {
            "outcome": IngressOutcome.FAILED.value,
            "run_id": run_id,
            "issue_key": issue_key,
            "comment_id": comment_id,
            "error": f"{type(exc).__name__}: {exc}",
            "problem": explain_read_failure(issue_key, exc),
        }


def _ignored(reason: IgnoreReason, run_id: str) -> dict[str, Any]:
    """Deliberate silence — one of the closed reasons, and nothing else."""
    logger.info(f"run {run_id}: ignored — {reason.value}")
    return {"outcome": IngressOutcome.IGNORED.value, "ignored": reason.name, "run_id": run_id}


def explain_read_failure(issue_key: str, exc: Exception) -> str:
    """The reply's Problems line: what went wrong, and what to check first."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 403:
            return (
                f"Jira returned 403 Forbidden for {issue_key}. This usually means the "
                "agent's Jira account does not have Browse Projects permission on this "
                "project. Jira returns the same error whether a ticket is missing or "
                "merely invisible to this account, so check the permission first."
            )
        if code == 404:
            return (
                f"Jira returned 404 Not Found for {issue_key}. The ticket may have been "
                "deleted or moved, or this account cannot see it — Jira answers the same "
                "for both, so check the agent account's access first."
            )
        if code == 401:
            return (
                "Jira rejected the agent's credentials (401 Unauthorized). The API token "
                "may have expired — an administrator must replace JIRA_API_TOKEN."
            )
        if code >= 500:
            return (
                f"Jira had a server error ({code}) while reading {issue_key}. This is "
                "usually temporary; asking again in a few minutes is safe."
            )
        return f"Jira returned {code} while reading {issue_key}: {exc.response.text[:200]}"
    if isinstance(exc, httpx.HTTPError):
        return f"Could not reach Jira to read {issue_key}: {type(exc).__name__}: {exc}"
    return f"Could not read {issue_key}: {type(exc).__name__}: {exc}"


@tool(name="classify_intent")
async def classify_intent(comment_body: str, issue_summary: str = "") -> dict[str, Any]:
    """Layer 2: one cheap model call over a CLOSED intent set.

    Returns ``{"scores": {...}}``, or ``{"scores": None}`` when the model could
    not be reached — which the workflow treats as "ask", never as a default
    route. Guessing BUILD on no information is the failure the whole ladder
    exists to prevent.
    """
    prompt = (
        "Classify what this Jira comment is asking for. Answer with ONLY a JSON "
        "object of confidences summing to 1.0, over exactly these keys: "
        '{"BUILD": 0.0, "REVIEW": 0.0, "QUESTION": 0.0, "CHATTER": 0.0}.\n\n'
        "BUILD    — asking for the work to be broken into Jira issues.\n"
        "REVIEW   — asking whether the requirement is clear, complete or ready.\n"
        "QUESTION — asking about this ticket, wanting an explanation.\n"
        "CHATTER  — anything else: greetings, questions about the world, "
        "comments not addressed to an automated system.\n\n"
        "A plain description of desired behaviour, with no explicit verb and "
        'no language questioning readiness or clarity (e.g. "let depot staff '
        'record a tyre pressure check"), is a request for the work — weight '
        "BUILD over REVIEW for it. Reserve REVIEW for comments that actually "
        "ask about completeness, gaps or readiness, not merely comments that "
        "happen to read like a spec.\n\n"
        f"Ticket: {issue_summary}\n"
        f"Comment: {comment_body}"
    )

    model = settings.classifier_model()
    try:
        # The AI Gateway client, exactly as the two child agents call it. The
        # previous `agent_lib.llm.llm_factory` path never reached the gateway —
        # every Layer 2 decision this router made fell through to "ask".
        from agent_lib.gateway.ai import AiGatewayClient  # activity-side only

        async with AiGatewayClient() as client:
            reply = await client.chat(
                provider=settings.provider_for_model(model),
                model_name=model,
                prompt=prompt,
                temperature=0.0,
                max_tokens=200,
            )
        text = (reply or {}).get("content") or ""
        scores = _parse_scores(text)
        if not scores:
            logger.warning(f"Classifier returned no usable scores: {text[:200]}")
            return {"scores": None, "error": "unparseable classifier response"}
        return {"scores": scores}
    except Exception as exc:  # noqa: BLE001 — unreachable model means "ask"
        logger.error(f"Classifier unavailable ({model}): {exc}", exc_info=True)
        return {"scores": None, "error": f"{type(exc).__name__}: {exc}"}


def _parse_scores(text: str) -> dict[str, float]:
    """The confidences out of a model response, tolerating prose around them."""
    import json
    import re

    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}

    valid = {"BUILD", "REVIEW", "QUESTION", "CHATTER"}
    out: dict[str, float] = {}
    for key, value in (data or {}).items():
        name = str(key).strip().upper()
        if name in valid:
            try:
                out[name] = float(value)
            except (TypeError, ValueError):
                continue
    return out


@tool(name="post_reply")
async def post_reply(
    issue_key: str,
    blocks: list[Any],
    run_id: str,
    comment_id: str = "",
    idempotency_key_value: str = "",
    label: str = "",
    thread_id: str = "",
) -> dict[str, Any]:
    """Post THE reply, stamp the label, record the answered comment.

    With ``thread_id`` the reply is posted INTO that comment's thread, the way
    Jira's own "Reply" does. ``parentId`` is what Jira's UI sends; it is not in
    the published REST docs (observed working on jiraagentdemo, 2026-09-24), so
    if Jira ever refuses it the reply is posted as a plain comment instead —
    a reply in the wrong place beats no reply.

    All three in one activity on purpose. They are one logical act — "this
    comment has been answered" — and splitting them leaves states where the
    reply exists but nothing remembers it, so a redelivery answers twice.

    The answered record is written **after** the comment posts, never before: a
    comment marked answered without a reply is silence the user cannot escape.
    """
    issue_key = (issue_key or "").strip().upper()
    if not issue_key:
        return {"posted": False, "error": "no issue key"}

    if settings.read_only():
        logger.info(f"run {run_id}: ORCH_READ_ONLY is on — not posting to {issue_key}")
        return {"posted": False, "read_only": True}

    pairs = [(str(b[0]), b[1]) for b in blocks]
    document = blocks_to_doc(pairs)

    try:
        async with _client() as client:
            url = f"/rest/api/3/issue/{issue_key}/comment"
            threaded = bool(thread_id)
            if threaded:
                resp = await client.post(url, json={"body": document, "parentId": thread_id})
                if resp.status_code == 400:
                    logger.warning(
                        f"run {run_id}: Jira refused a reply under comment {thread_id} "
                        f"({resp.text[:200]}) — posting it as a plain comment"
                    )
                    threaded = False
            if not threaded:
                resp = await client.post(url, json={"body": document})
            resp.raise_for_status()
            posted_id = str(resp.json().get("id") or "")
            where = f"under comment {thread_id}" if threaded else "as a new comment"
            logger.info(f"run {run_id}: posted comment {posted_id} on {issue_key} {where}")

            # Only now is the request genuinely answered.
            recorded = await _record_answered(
                client, issue_key, [idempotency_key_value, comment_id]
            )
            labels = await _add_label(client, issue_key, label) if label else []

            return {
                "posted": True,
                "comment_id": posted_id,
                "threaded": threaded,
                "recorded": recorded,
                "labels": labels,
            }
    except Exception as exc:  # noqa: BLE001 — a failed post is reported, never raised
        logger.error(f"run {run_id}: could not post to {issue_key}: {exc}", exc_info=True)
        return {"posted": False, "error": str(exc)}


async def _record_answered(client: httpx.AsyncClient, issue_key: str, keys: list[str]) -> bool:
    wanted = {k for k in keys if k}
    if not wanted:
        return False
    try:
        existing = await _answered_ids(client, issue_key)
        existing |= wanted
        # Keep the list bounded on a long-lived ticket.
        ids = sorted(existing, key=lambda v: (len(v), v))[-200:]
        resp = await client.put(
            f"/rest/api/3/issue/{issue_key}/properties/{settings.ANSWERED_PROPERTY}",
            json={"ids": ids},
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning(f"Could not record answered comment on {issue_key}: {exc}")
        return False


async def _add_label(client: httpx.AsyncClient, issue_key: str, label: str) -> list[str]:
    try:
        resp = await client.put(
            f"/rest/api/3/issue/{issue_key}", json={"update": {"labels": [{"add": label}]}}
        )
        resp.raise_for_status()
        return [label]
    except httpx.HTTPError as exc:
        logger.warning(f"Could not stamp {label!r} on {issue_key}: {exc}")
        return []


@tool(name="notify_admins")
async def notify_admins(subject: str, body: str, run_id: str) -> dict[str, Any]:
    """Tell the administrators the router itself could not do its job — a child
    that never answered, or a reply that would not post.

    Always logged. Emailed too, through the shared sender; the result says which,
    and a reply may only say "Emailed" when ``sent`` is true.
    """
    recipients = list(settings.notify_emails())
    logger.error(f"run {run_id}: ADMIN ALERT to {', '.join(recipients) or '-'} — {subject}: {body}")
    if not recipients:
        return {"attempted": False, "sent": False, "error": "ORCH_NOTIFY_EMAILS is not set"}
    delivery = await deliver(
        recipients,
        f"[Aetherion] ALERT — {subject}",
        f"{body}\n\nrun {run_id}",
        repeat_key=("admin", subject, body),
        repeat_window_seconds=settings.email_repeat_window_seconds(),
    )
    return _delivery_dict(delivery)


@tool(name="send_outcome_email")
async def send_outcome_email(
    kind: str,
    issue_key: str,
    headline: str,
    details: list[str],
    run_id: str,
) -> dict[str, Any]:
    """The one outcome email for a run, decided by the router.

    ``kind`` is created / clarification / duplicates / failed; the workflow maps
    the child's outcome to it and never passes one for an invalid request. This
    activity decides whether ``ORCH_NOTIFY_ON`` wants it, because a workflow may
    not read the environment.
    """
    if kind not in settings.notify_on():
        return {"attempted": False, "sent": False, "skipped": f"{kind} is not in ORCH_NOTIFY_ON"}
    base, _, _ = settings.jira_credentials()
    link = f"{base}/browse/{issue_key}" if base and issue_key else issue_key
    body = "\n".join(
        [headline, "", link, "", *[f"- {d}" for d in details if d], "", f"run {run_id}"]
    )
    delivery = await deliver(
        list(settings.notify_emails()),
        f"[Aetherion] {issue_key} — {headline}",
        body,
        # The details are part of what makes an email "the same": BGV-3's second
        # clarification asked different questions and was held back as a repeat.
        repeat_key=("outcome", kind, issue_key, headline, tuple(details)),
        repeat_window_seconds=settings.email_repeat_window_seconds(),
    )
    return _delivery_dict(delivery)


@tool(name="smtp_login_check")
async def smtp_login_check() -> dict[str, Any]:
    """Health check: can the router's Gmail account log in? Sends nothing."""
    return await check_login()


def _delivery_dict(delivery: Delivery) -> dict[str, Any]:
    """JSON-safe, for crossing back into the workflow."""
    return {
        "attempted": delivery.attempted,
        "sent": delivery.sent,
        "recipients": delivery.recipients,
        "error": delivery.error,
        "suppressed_repeat": delivery.suppressed_repeat,
    }
