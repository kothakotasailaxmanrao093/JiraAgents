"""Database helpers for the Jira Requirement Review agents.

Currently only resolves recipient emails to `public."user".id` UUIDs for the
Pulsar notification endpoint — ported from the sibling `my_first_agent`'s
`db_handler.py` (itself adapted from the PACER CC agent). Kept tiny on purpose:
the only DB need here is turning operator-friendly emails into the UUIDs Pulsar
expects.
"""

from __future__ import annotations

import logging

from common_lib.database.connection import db
from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()
logger = logging.getLogger(__name__)

# Hit-only cache scoped per worker process. Misses are NOT cached so a newly
# seeded user is picked up on the next notification without a worker restart.
_user_id_by_email_cache: dict[str, str] = {}


def get_user_ids_by_emails(emails: list[str]) -> list[str]:
    """
    Resolve a list of email addresses to active `public."user".id` UUIDs.

    Emails that don't match an active user are dropped with a WARNING log
    (so typos / unseeded users surface without failing the notification).
    Order is not preserved; duplicates are de-duplicated.
    """
    if not emails:
        return []

    cached_ids: list[str] = []
    uncached: list[str] = []
    seen: set[str] = set()
    for raw in emails:
        email = raw.strip()
        if not email or email in seen:
            continue
        seen.add(email)
        hit = _user_id_by_email_cache.get(email)
        if hit is not None:
            cached_ids.append(hit)
        else:
            uncached.append(email)

    if not uncached:
        return cached_ids

    session = db.get_session()
    try:
        rows = session.execute(
            text(
                'SELECT email, id FROM public."user" '
                "WHERE email = ANY(:emails) AND COALESCE(is_active, TRUE) = TRUE"
            ),
            {"emails": uncached},
        ).fetchall()
        found: dict[str, str] = {row[0]: str(row[1]) for row in rows}
    except Exception as e:
        logger.error("Error looking up user IDs by email: %s", e, exc_info=True)
        return cached_ids
    finally:
        session.close()

    for email in uncached:
        uid = found.get(email)
        if uid is None:
            logger.warning(
                "Email '%s' not found in public.\"user\" (or is_active=false) — "
                "skipping for notification recipients",
                email,
            )
            continue
        _user_id_by_email_cache[email] = uid
        cached_ids.append(uid)

    return cached_ids
