"""Receive Jira webhooks and start the work-breakdown agent.

This is the piece that turns "write a ticket mentioning @Aetherion" into a run.
Jira Automation sends one HTTP request here; this service decides whether it is
worth acting on, and if so runs the agent in the background.

It is a plain FastAPI application, so it runs anywhere Python does:

    python -m uvicorn src.webhook.server:app --host 0.0.0.0 --port 8000

Four filters stand between a delivery and a run, and every one of them exists
to stop a loop:

1. **Shared secret** — a request without the right token is refused outright.
2. **Author** — anything the agent's own Jira account did is ignored, so the
   write-back comment and the marker labels cannot re-trigger it.
3. **Keyword** — the trigger word must appear somewhere in the event.
4. **Debounce** — the same issue is not started twice within a short window,
   which collapses the burst of events a single edit produces.

A run that gets past all four is executed **in the background**, and the
response returns immediately: Jira retries anything that does not answer
quickly, and a retry here would mean a second run.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import pathlib
import time
from typing import Any

from common_lib.utils.logger import setup_logger
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

# Run as a standalone service (`uvicorn src.webhook.server:app`) nothing else
# has loaded `.env`, so the Jira credentials would be missing. `load_dotenv`
# never overrides a variable the environment already set, so a deployment that
# injects its own configuration is unaffected.
load_dotenv(pathlib.Path(__file__).resolve().parents[2] / ".env")

# Imported after load_dotenv on purpose: these read configuration at import time.
from src.agent.agent import JiraTaskCreation  # noqa: E402
from src.config.settings import env_bool  # noqa: E402
from src.jira import api as jira  # noqa: E402
from src.jira.client import IDEMPOTENCY_LABEL_PREFIX as _LTW  # noqa: E402
from src.tools import tools as tool_module  # noqa: E402

logger = setup_logger(__name__)

DEFAULT_DEBOUNCE_SECONDS = 20.0
DEFAULT_MAX_CONCURRENT_RUNS = 2

# The agent addresses tools by name; this is the local dispatch table used when
# the service runs the agent in-process rather than through Temporal.
TOOLS = {
    "read_jira_issue": tool_module.read_jira_issue,
    "inspect_jira_context": tool_module.inspect_jira_context,
    "validate_requirement": tool_module.validate_requirement,
    "generate_work_breakdown": tool_module.generate_work_breakdown,
    "create_jira_issues": tool_module.create_jira_issues,
    "notify_email": tool_module.notify_email,
    "report_to_issue": tool_module.report_to_issue,
}

app = FastAPI(
    title="Work Breakdown webhook receiver",
    description="Receives Jira webhooks and starts the work-breakdown agent.",
)

# issue key -> the time a run last started for it
_recent_runs: dict[str, float] = {}
_run_lock = asyncio.Lock()
_slots: asyncio.Semaphore | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def webhook_secret() -> str:
    """Plain shared secret, sent as a header. Empty means the check is skipped.

    This is the style a **Jira Automation** rule uses, because there you can set
    an arbitrary header (``X-LTW-Secret``).
    """
    return os.environ.get("LTW_WEBHOOK_SECRET", "").strip()


def jira_signing_secret() -> str:
    """Secret for a Jira **System Webhook**, which signs instead of sending.

    Jira does not transmit this value. It uses it to compute an HMAC of the
    request body and sends the result in ``X-Hub-Signature``, so the check is a
    signature comparison rather than a string match.
    """
    return os.environ.get("LTW_JIRA_WEBHOOK_SECRET", "").strip()


def verify_signature(body: bytes, header: str, secret: str) -> bool:
    """Check Jira's ``X-Hub-Signature`` against the raw request body.

    The header is ``method=signature``, e.g. ``sha256=a4771c39...``. The method
    is read from the header rather than assumed, because Atlassian says it may
    change.
    """
    if not header or "=" not in header or not secret:
        return False
    method, _, supplied = header.partition("=")
    try:
        expected = hmac.new(secret.encode("utf-8"), body, method.strip().lower()).hexdigest()
    except (ValueError, TypeError):
        logger.warning(f"Unsupported webhook signature method: {method!r}")
        return False
    return hmac.compare_digest(expected, supplied.strip())


def debounce_seconds() -> float:
    raw = os.environ.get("LTW_WEBHOOK_DEBOUNCE_SECONDS", "").strip()
    try:
        return float(raw) if raw else DEFAULT_DEBOUNCE_SECONDS
    except ValueError:
        logger.warning("LTW_WEBHOOK_DEBOUNCE_SECONDS is not a number; using the default")
        return DEFAULT_DEBOUNCE_SECONDS


def max_concurrent_runs() -> int:
    raw = os.environ.get("LTW_WEBHOOK_MAX_CONCURRENT", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_CONCURRENT_RUNS
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_RUNS


def _slot_semaphore() -> asyncio.Semaphore:
    """One semaphore for the process, created lazily inside the event loop."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(max_concurrent_runs())
    return _slots


# ---------------------------------------------------------------------------
# Reading the Jira event
# ---------------------------------------------------------------------------


def issue_key_from(payload: dict[str, Any]) -> str:
    """The issue this event is about.

    Jira Automation is normally configured to send ``{"issue_key": "ABC-1"}``,
    but a raw Jira webhook nests it under ``issue``. Both are accepted so the
    service works either way.
    """
    direct = payload.get("issue_key") or payload.get("issueKey")
    if direct:
        return str(direct).strip().upper()
    issue = payload.get("issue") or {}
    return str(issue.get("key", "")).strip().upper()


def actor_of(payload: dict[str, Any]) -> tuple[str, str]:
    """Who caused this event, as ``(account_id, email)``.

    Checked against the agent's own account so its write-back cannot re-trigger
    it. Jira puts the actor in different places depending on the event.
    """
    for node in (
        (payload.get("comment") or {}).get("author"),
        payload.get("user"),
        (payload.get("issue") or {}).get("fields", {}).get("reporter"),
    ):
        if isinstance(node, dict) and (node.get("accountId") or node.get("emailAddress")):
            return (
                str(node.get("accountId") or "").strip(),
                str(node.get("emailAddress") or "").strip().lower(),
            )
    return "", ""


# Fields the agent changes as bookkeeping rather than as work.
_BOOKKEEPING_FIELDS = {"labels", "comment"}


def is_agent_reply(payload: dict[str, Any]) -> bool:
    """True when the event carries a comment this agent wrote.

    This is the loop guard. Identity cannot answer it — the agent usually runs
    as the same Jira account as the people using it — but the signature can:
    every reply contains "AetherionAgent", which no person types by accident.
    """
    body = jira._plain_text((payload.get("comment") or {}).get("body"))
    return jira.is_agent_comment(body)


def comment_id_of(payload: dict[str, Any]) -> str:
    """The id of the comment in this event, if it carries one."""
    return str((payload.get("comment") or {}).get("id") or "").strip()


def is_agent_bookkeeping(payload: dict[str, Any], event: str) -> bool:
    """True when an event is the agent tidying up after itself.

    Needed because the agent often runs as the **same Jira account as the
    person using it**. Ignoring everything that account does would ignore the
    human too — which is exactly what happened on project TT2: every ticket a
    person created was dropped as "caused by the agent itself".

    So instead of asking *who*, this asks *what*:

    * a comment — the agent's write-back;
    * an update that touched only labels — the marker labels;
    * a newly created issue that already carries an ``ltw-`` label — a ticket
      the agent just created.

    Anything else from that account is a person doing something, and runs.
    """
    lowered = event.lower()

    if "comment" in lowered:
        # Only our own replies. A person's comment on the same account is a
        # request — treating every comment as ours ignored the human too.
        return is_agent_reply(payload)

    items = (payload.get("changelog") or {}).get("items") or []
    if items and all(
        str(item.get("field", "")).strip().lower() in _BOOKKEEPING_FIELDS for item in items
    ):
        return True

    if "created" in lowered:
        labels = ((payload.get("issue") or {}).get("fields") or {}).get("labels") or []
        if any(str(label).startswith(_LTW) for label in labels):
            return True

    return False


def event_text(payload: dict[str, Any]) -> str:
    """Every piece of text in the event that could carry the keyword."""
    parts: list[str] = []
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    parts.append(str(fields.get("summary") or ""))
    parts.append(jira._plain_text(fields.get("description")))
    parts.append(jira._plain_text((payload.get("comment") or {}).get("body")))
    # Automation rules often send the text directly rather than the whole issue.
    for key in ("summary", "description", "text", "requirement"):
        if isinstance(payload.get(key), str):
            parts.append(payload[key])
    return "\n".join(p for p in parts if p)


# Cached agent account id, plus when a failed lookup was last attempted.
_ACCOUNT_RETRY_SECONDS = 60.0
_account_cache: dict[str, Any] = {"value": "", "failed_at": 0.0}


async def _agent_account_id() -> str:
    """The account id the agent writes as, cached after a successful lookup.

    A failed lookup is **not** cached permanently. Caching "" meant one blip in
    Jira left the account half of the loop guard disabled until the process was
    restarted, silently. A failure is retried after a short delay instead.
    """
    if _account_cache["value"]:
        return str(_account_cache["value"])
    if (time.monotonic() - float(_account_cache["failed_at"])) < _ACCOUNT_RETRY_SECONDS:
        return ""

    base_url, email, token = jira.jira_creds()
    account_id = ""
    if base_url and email and token:
        try:
            async with jira.jira_client(base_url, email, token) as client:
                resp = await client.get("/rest/api/3/myself")
                if resp.status_code == 200:
                    account_id = str(resp.json().get("accountId", ""))
        except Exception as exc:  # noqa: BLE001 — never fail a delivery over this
            logger.warning(f"Could not resolve the agent's own account id: {exc}")
    if account_id:
        _account_cache["value"] = account_id
    else:
        _account_cache["failed_at"] = time.monotonic()
    return account_id


# ---------------------------------------------------------------------------
# Running the agent
# ---------------------------------------------------------------------------


def local_execution() -> bool:
    """True when this process runs the agent itself instead of the platform.

    Defaults to true, which is what this service has always done. Set it to
    false once runs are started through the Aetherion platform, so the SDK's
    own executor is left alone.
    """
    return env_bool("LTW_LOCAL_EXECUTION", default=True)


_executor_installed = False


def install_local_executor() -> None:
    """Point the SDK's tool executor at the in-process dispatch table, once.

    This used to run on every request, rewriting a process-wide SDK object each
    time. Doing it once at startup makes the mutation explicit and leaves the
    platform executor intact when local execution is switched off.
    """
    global _executor_installed
    if _executor_installed or not local_execution():
        return

    async def execute(name: str, *args: Any, **_kwargs: Any) -> Any:
        return await TOOLS[name](*args)

    import src.agent.agent as agent_module

    agent_module.toolExecutor.execute = execute
    _executor_installed = True
    logger.info("Local tool executor installed; the agent runs in this process.")


async def run_agent(issue_key: str, comment_id: str = "") -> dict[str, Any]:
    """Run the agent once, in this process, with the real tools."""
    install_local_executor()  # no-op after the first call; covers direct callers
    payload: dict[str, Any] = {"issue_key": issue_key}
    if comment_id:
        # Answer the comment that asked, not merely the newest one.
        payload["comment_id"] = comment_id
    return await JiraTaskCreation.fn(payload)


async def _report_failure(issue_key: str, comment_id: str, exc: Exception) -> None:
    """Tell the requester on the issue that the run died.

    Without this a crash before the agent's own reporting — a permissions error
    reading the issue, say — left the person who typed the trigger with total
    silence, and the only trace in a log file they cannot see.
    """
    try:
        await tool_module.report_to_issue(
            issue_key=issue_key,
            headline="This request could not be completed.",
            situation=(
                "The run stopped with an unexpected error before a work breakdown "
                "could be produced, so no Jira issues were created. Comment again "
                "to retry."
            ),
            errors=[f"{type(exc).__name__}: {exc}"],
            answered_comment_id=comment_id or None,
        )
    except Exception as report_exc:  # noqa: BLE001 — reporting must never raise
        logger.error(f"{issue_key}: could not report the failure on the issue: {report_exc}")


async def _run_in_background(issue_key: str, comment_id: str = "") -> None:
    """Execute one run, bounded by the concurrency limit. Never raises."""
    async with _slot_semaphore():
        try:
            result = await run_agent(issue_key, comment_id)
            logger.info(f"{issue_key}: {result.get('status')} — {result.get('message', '')[:160]}")
        except Exception as exc:  # noqa: BLE001 — a bad run must not kill the service
            logger.error(f"{issue_key}: run failed with an unhandled error: {exc}")
            await _report_failure(issue_key, comment_id, exc)


async def _should_start(issue_key: str) -> bool:
    """False when a run for this issue started moments ago.

    A single edit in Jira can produce several deliveries. Without this, each one
    would start its own run; they would race, and the slowest would find the
    work the fastest had already created.
    """
    window = debounce_seconds()
    now = time.monotonic()
    async with _run_lock:
        last = _recent_runs.get(issue_key)
        if last is not None and (now - last) < window:
            return False
        _recent_runs[issue_key] = now
        # Keep the table small; entries older than the window are meaningless.
        for key, when in list(_recent_runs.items()):
            if (now - when) > max(window * 5, 300):
                _recent_runs.pop(key, None)
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def index() -> dict[str, Any]:
    """What this service is, for anyone who opens the URL in a browser.

    Worth having: the first thing you do after starting a tunnel is paste the
    public URL into a browser, and a bare 404 tells you nothing about whether
    the tunnel or the service is at fault.
    """
    return {
        "service": "Work Breakdown webhook receiver",
        "purpose": (
            "Receives Jira webhooks and starts the work-breakdown agent when an "
            f"issue mentions '{jira.trigger_keyword()}'."
        ),
        "endpoints": {
            "GET /": "this message",
            "GET /health": "configuration and readiness",
            "POST /jira/webhook": "where Jira sends its events",
        },
        "jira_webhook_url": "point your Jira webhook at <this-url>/jira/webhook",
    }


@app.post("/")
async def posted_to_the_root(request: Request) -> dict[str, Any]:
    """Explain the mistake when a webhook is pointed at the bare domain.

    Configuring the URL without the path is the single easiest thing to get
    wrong, and the default answer — 405 Method Not Allowed — says nothing about
    what to fix. Jira's webhook admin page shows this body against the failed
    delivery, so the fix is visible where the mistake was made.
    """
    correct = str(request.base_url).rstrip("/") + "/jira/webhook"
    logger.warning(
        f"A webhook was POSTed to '/' instead of '/jira/webhook'. "
        f"Update the Jira webhook URL to: {correct}"
    )
    raise HTTPException(
        status_code=404,
        detail=(
            "Wrong path. Webhooks must be sent to /jira/webhook, not /. "
            f"Update the URL in Jira to: {correct}"
        ),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus the settings that decide whether a delivery is acted on."""
    from src.notifications import email as notifier

    base_url, email, token = jira.jira_creds()
    return {
        "status": "ok",
        "jira_configured": bool(base_url and email and token),
        "jira_base_url": base_url,
        "trigger_keyword": jira.trigger_keyword(),
        "secret_required": bool(webhook_secret() or jira_signing_secret()),
        "authenticated": bool(webhook_secret() or jira_signing_secret()),
        "local_execution": local_execution(),
        "header_secret_configured": bool(webhook_secret()),
        "jira_signature_configured": bool(jira_signing_secret()),
        "debounce_seconds": debounce_seconds(),
        "max_concurrent_runs": max_concurrent_runs(),
        "email_configured": notifier.is_configured(),
        "email_recipients": notifier.recipients(),
    }


@app.post("/jira/webhook")
async def jira_webhook(
    request: Request,
    background: BackgroundTasks,
    x_ltw_secret: str | None = Header(default=None),
    x_hub_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    """Receive one Jira event and decide whether to start a run.

    Always answers immediately. Jira retries a slow endpoint, and a retry would
    mean a second run, so the work happens in the background.
    """
    # The raw bytes are needed before parsing: the signature covers the body
    # exactly as sent, so re-serialising the parsed JSON would not match.
    raw_body = await request.body()

    header_secret = webhook_secret()
    signing_secret = jira_signing_secret()

    if not (header_secret or signing_secret):
        # Fail closed. This endpoint creates Jira issues, so an unauthenticated
        # deployment is an open write endpoint. Previously a blank secret simply
        # skipped the check, which is the wrong direction to fail in.
        logger.error(
            "Refusing a delivery: neither LTW_WEBHOOK_SECRET nor "
            "LTW_JIRA_WEBHOOK_SECRET is set, so deliveries cannot be "
            "authenticated. Set one and restart."
        )
        raise HTTPException(status_code=503, detail="webhook authentication is not configured")

    if header_secret or signing_secret:
        # Either style is accepted, so one deployment can take deliveries from a
        # Jira Automation rule and a System Webhook at the same time.
        supplied = x_ltw_secret or request.query_params.get("secret") or ""
        # Constant-time compare: a plain `!=` leaks the secret a character at a
        # time to anyone who can measure the response.
        by_header = bool(header_secret) and hmac.compare_digest(supplied, header_secret)
        by_signature = bool(signing_secret) and verify_signature(
            raw_body, x_hub_signature or "", signing_secret
        )
        if not (by_header or by_signature):
            logger.warning(
                "Rejected a webhook delivery: no valid X-LTW-Secret header and "
                "no valid X-Hub-Signature"
            )
            raise HTTPException(status_code=401, detail="invalid webhook secret")

    try:
        payload = json.loads(raw_body)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    issue_key = issue_key_from(payload)
    event = str(payload.get("webhookEvent") or payload.get("issue_event_type_name") or "unknown")

    if not issue_key:
        logger.info(f"Ignored {event}: no issue key in the payload")
        return {"accepted": False, "reason": "no_issue_key"}

    # --- loop prevention ---------------------------------------------------
    # Only the agent's own *bookkeeping* is ignored, not everything its account
    # does. The two are frequently the same account.
    actor_id, actor_email = actor_of(payload)
    own_id = await _agent_account_id()
    _, own_email, _ = jira.jira_creds()
    by_agent_account = (own_id and actor_id and actor_id == own_id) or (
        own_email and actor_email and actor_email == own_email.lower()
    )
    if by_agent_account and is_agent_bookkeeping(payload, event):
        logger.info(f"Ignored {event} on {issue_key}: the agent's own bookkeeping")
        return {"accepted": False, "reason": "own_change", "issue_key": issue_key}

    # --- the keyword --------------------------------------------------------
    text = event_text(payload)
    # An Automation rule that sends only {"issue_key": ...} carries no text at
    # all. Rather than refuse it, let the agent read the issue and decide —
    # read_jira_issue applies exactly the same keyword rule.
    if text and not jira.mentions_trigger(text):
        logger.info(f"Ignored {event} on {issue_key}: no '{jira.trigger_keyword()}' mention")
        return {"accepted": False, "reason": "no_keyword", "issue_key": issue_key}

    # --- debounce -----------------------------------------------------------
    # Keyed per comment, so two different requests on one ticket both run while
    # repeat deliveries of the same one do not.
    comment_id = comment_id_of(payload)
    debounce_key = f"{issue_key}#{comment_id}" if comment_id else issue_key
    if not await _should_start(debounce_key):
        logger.info(f"Ignored {event} on {issue_key}: a run started moments ago")
        return {"accepted": False, "reason": "debounced", "issue_key": issue_key}

    logger.info(f"Accepted {event} on {issue_key}; starting a run")
    background.add_task(_run_in_background, issue_key, comment_id)
    return {
        "accepted": True,
        "issue_key": issue_key,
        "event": event,
        "comment_id": comment_id,
    }
