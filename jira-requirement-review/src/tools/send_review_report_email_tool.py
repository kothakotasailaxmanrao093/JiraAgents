"""@tool: send a templated email notification via the Pulsar notification service.

Ported from the sibling `my_first_agent`'s `jira_notification_activities.py` —
the same mechanism the PACER CC agent uses at its lifecycle checkpoints.
Best-effort: this activity never raises, so a notification problem can't fail
the review run (``review_flow._send_report_email`` also wraps its call, so a
failure here degrades to ``email_status: "error"`` rather than a broken run).

Recipients are operator-friendly: set ``JIRA_NOTIFY_EMAILS`` to a comma-separated
list of emails (resolved to UUIDs against ``public."user"``). For environments
where the user table isn't seeded, ``JIRA_NOTIFY_USER_IDS`` may instead carry
UUIDs directly, which skips the DB lookup.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import aiohttp
from aetherion_sdk import tool
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from utils import db_handler

load_dotenv()
logger = logging.getLogger(__name__)


class NotificationPayload(BaseModel):
    """Mirrors Pulsar's TriggerNotificationRequest schema."""

    template_name: str = Field(..., min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    to: list[UUID] = Field(..., min_length=1)
    channels: list[str] | None = None

    model_config = ConfigDict(extra="forbid")


def _resolve_recipients() -> list[str]:
    """
    Resolve notification recipients to UUIDs.

    Precedence:
      1. JIRA_NOTIFY_USER_IDS — comma-separated UUIDs, used as-is (no DB lookup).
      2. JIRA_NOTIFY_EMAILS    — comma-separated emails, resolved to UUIDs via
                                 public."user".
    """
    direct = os.environ.get("JIRA_NOTIFY_USER_IDS", "").strip()
    if direct:
        return [u.strip() for u in direct.split(",") if u.strip()]

    env_value = os.environ.get("JIRA_NOTIFY_EMAILS", "").strip()
    if not env_value:
        return []
    emails = [e.strip() for e in env_value.split(",") if e.strip()]
    if not emails:
        return []
    return db_handler.get_user_ids_by_emails(emails)


@tool(name="send_review_report_email")
async def send_review_report_email(
    template_name: str,
    context: dict[str, Any],
    channels: list[str] | None = None,
) -> dict[str, Any]:
    """
    Send a templated email notification via Pulsar.

    Best-effort: never raises. Returns a dict with `status`:
      - `sent`    — accepted by Pulsar
      - `skipped` — no recipients or NOTIFICATIONS_BASE_URL not set
      - `error`   — invalid payload, non-200 response, or transport exception

    Args:
        template_name: Pulsar template name (must be registered on the platform,
            e.g. "jira_requirement_review_report").
        context: Jinja2 template variables (report metrics, download link, etc).
        channels: Optional channel override (e.g. ["email"]); None lets Pulsar
            pick per-user defaults.
    """
    recipients = _resolve_recipients()
    if not recipients:
        logger.info(
            "Skipping notification '%s': no recipients configured "
            "(set JIRA_NOTIFY_EMAILS to a comma-separated list of user emails, or "
            "JIRA_NOTIFY_USER_IDS to UUIDs)",
            template_name,
        )
        return {"status": "skipped", "reason": "no_recipients"}

    base = os.environ.get("NOTIFICATIONS_BASE_URL", "").rstrip("/")
    if not base:
        logger.warning(
            "Skipping notification '%s': NOTIFICATIONS_BASE_URL is not set", template_name
        )
        return {"status": "skipped", "reason": "no_base_url"}

    url = f"{base}/api/v1/pulsar/external/trigger-notification"

    enriched_context = {
        "current_date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
        **context,
    }

    try:
        payload = NotificationPayload(
            template_name=template_name,
            context=enriched_context,
            to=recipients,
            channels=channels,
        )
    except ValidationError as e:
        logger.error("Notification payload validation failed for '%s': %s", template_name, e)
        return {"status": "error", "reason": "invalid_payload", "error": str(e)}

    logger.info(
        "Triggering notification '%s' to %d recipient(s) at %s",
        template_name,
        len(recipients),
        url,
    )

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload.model_dump(mode="json")) as response:
                body = await response.text()
                if response.status != 200:
                    logger.error(
                        "Notification API returned non-200 for '%s': status=%d, body=%s",
                        template_name,
                        response.status,
                        body[:500],
                    )
                    return {
                        "status": "error",
                        "reason": "non_200",
                        "http_status": response.status,
                        "body_excerpt": body[:500],
                    }
        logger.info("Notification '%s' sent to %d recipient(s)", template_name, len(recipients))
        return {
            "status": "sent",
            "template_name": template_name,
            "recipient_count": len(recipients),
        }
    except Exception as e:
        logger.error("Failed to send notification '%s': %s", template_name, e, exc_info=True)
        return {"status": "error", "reason": "exception", "error": str(e)}
