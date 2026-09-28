"""Email delivery — the ONE sender every agent uses.

Moved here from the work-breakdown agent's ``notifications/email.py``, where it
had been running correctly for months, so that the router can send too. Before
this, the router had no sender at all: children stood down in delegated mode on
the understanding the router would send, and it could not (FLAWS.md F37).

Only delivery lives here: the SMTP connection, the credentials, and repeat
suppression. What a message *says* stays with each agent, because each has
different facts to report. Nothing here imports agent code, so every agent can
mirror it unchanged.

Activity-side only: it reads the environment and opens a network connection,
neither of which a Temporal workflow may do.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import sys
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from ._logging import get_logger
from .settings import mask

logger = get_logger(__name__)

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_TIMEOUT = 30.0


@dataclass
class Delivery:
    """What happened to one email. ``sent`` is the only field a reply may cite
    as "emailed" — ``attempted`` means we tried, not that it arrived."""

    attempted: bool = False
    sent: bool = False
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    error: str = ""
    suppressed_repeat: bool = False


def smtp_settings() -> tuple[str, int, str, str]:
    """Host, port and the sending account, from the environment."""
    host = os.environ.get("GMAIL_SMTP_HOST", "").strip() or DEFAULT_SMTP_HOST
    raw_port = os.environ.get("GMAIL_SMTP_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_SMTP_PORT
    except ValueError:
        logger.warning(f"GMAIL_SMTP_PORT is not a number ({raw_port!r}); using {DEFAULT_SMTP_PORT}")
        port = DEFAULT_SMTP_PORT
    sender = os.environ.get("GMAIL_SENDER", "").strip()
    # A Google account password will not work here: Gmail requires a 16-character
    # app password with 2-Step Verification enabled.
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    return host, port, sender, password


# --------------------------------------------------------------------------
# Not mailing the same person the same thing twice
# --------------------------------------------------------------------------
# A failing workflow is retried by Temporal, and every retry reaches here with
# identical arguments; left alone that is one email per retry. The ledger is per
# worker process: no storage, and a retry storm is served by one worker, which is
# the case that hurts. It hangs off ``sys`` because a module can be imported
# twice in one process (source tree and installed package) with two globals.

_LEDGER_ATTR = "_aetherion_email_recent_sends"


def _ledger() -> dict[tuple[str, ...], float]:
    store = getattr(sys, _LEDGER_ATTR, None)
    if store is None:
        store = {}
        setattr(sys, _LEDGER_ATTR, store)
    return store


def is_repeat(key: tuple[str, ...], window_seconds: float) -> bool:
    """True when this exact message already went out inside the window."""
    if window_seconds <= 0:
        return False
    now = time.monotonic()
    recent = _ledger()
    for stale, sent_at in list(recent.items()):
        if now - sent_at > window_seconds:
            del recent[stale]
    return key in recent


def remember(key: tuple[str, ...], window_seconds: float) -> None:
    """Record a message that actually left — never one that did not."""
    if window_seconds > 0:
        _ledger()[key] = time.monotonic()


def forget_recent_sends() -> None:
    """Clear the ledger. For tests."""
    _ledger().clear()


def send_sync(
    host: str, port: int, sender: str, password: str, to: list[str], subject: str, body: str
) -> None:
    """Blocking SMTP send. Called from a worker thread."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender, password)
        server.send_message(message)


def login_sync(host: str, port: int, sender: str, password: str) -> None:
    """Blocking SMTP login, nothing sent — the health check's probe.

    Google expires an app password when 2-Step Verification is reset, and an
    unused one can die unnoticed; this surfaces it before an email goes missing.
    """
    with smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(sender, password)


async def check_login() -> dict[str, Any]:
    """Can the configured account log in? Never raises; never sends."""
    host, port, sender, password = smtp_settings()
    if not (sender and password):
        return {"ok": False, "error": "GMAIL_SENDER or GMAIL_APP_PASSWORD is not set."}
    try:
        await asyncio.to_thread(login_sync, host, port, sender, password)
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        return {"ok": False, "error": explain_failure(exc)}
    return {"ok": True, "error": ""}


def explain_failure(exc: Exception) -> str:
    """One sentence a person can act on."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        # By far the most common: the account password instead of a
        # 16-character app password, or 2-Step Verification not enabled.
        return (
            f"Gmail rejected the credentials ({exc.smtp_code}). Use a 16-character "
            f"app password from https://myaccount.google.com/apppasswords, not the "
            f"account password."
        )
    return f"Email send failed: {type(exc).__name__}: {exc}"


async def deliver(
    to: list[str],
    subject: str,
    body: str,
    *,
    repeat_key: tuple[str, ...] | None = None,
    repeat_window_seconds: float = 0.0,
) -> Delivery:
    """Send one message. Never raises; the result says exactly what happened."""
    host, port, sender, password = smtp_settings()

    if repeat_key and is_repeat(repeat_key, repeat_window_seconds):
        logger.info(f"Email suppressed as a repeat within {int(repeat_window_seconds)}s: {subject}")
        return Delivery(subject=subject, recipients=list(to), suppressed_repeat=True)

    logger.info(f"GMAIL_SENDER: {sender or '<empty>'} | GMAIL_APP_PASSWORD: {mask(password)}")
    if not (sender and password):
        return Delivery(
            subject=subject, error="Email not sent: GMAIL_SENDER or GMAIL_APP_PASSWORD is not set."
        )
    if not to:
        return Delivery(subject=subject, error="Email not sent: no recipients are configured.")

    try:
        await asyncio.to_thread(send_sync, host, port, sender, password, to, subject, body)
    except Exception as exc:  # noqa: BLE001 — a mail failure must not fail the run
        reason = explain_failure(exc)
        logger.error(reason)
        return Delivery(attempted=True, recipients=list(to), subject=subject, error=reason)

    if repeat_key:
        remember(repeat_key, repeat_window_seconds)
    logger.info(f"Email sent to {len(to)} recipient(s): {subject}")
    return Delivery(attempted=True, sent=True, recipients=list(to), subject=subject)
