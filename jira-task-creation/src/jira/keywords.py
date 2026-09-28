"""Pure trigger vocabulary. Safe to import from workflow code.

Nothing here touches the network or the Jira client, so the ingest side can ask
what the trigger word is without pulling in httpx and the whole REST layer.
:mod:`src.jira.trigger` re-exports every name, so callers reaching the facade
through ``src.jira.api`` see exactly these objects.

The *identity* half — the signature a reply carries and the rule for spotting
the system's own comments — now lives in :mod:`src.shared.keywords`, because the
whole system must sign with one voice: once the router posts every reply, a
per-agent signature would leave a human no reliable way to recognise an
automated comment. What stays here is what is genuinely this agent's: the
environment variables it reads and the labels it stamps.
"""

from __future__ import annotations

import os

from src.shared.keywords import (
    AGENT_FOOTER,
    AGENT_SIGNATURE,
    DEFAULT_TRIGGER_KEYWORD,
    is_agent_comment,
)
from src.shared.keywords import mentions_trigger as _mentions_trigger

# Where the ids of already-answered comments are kept. A Jira issue property is
# storage Jira provides for exactly this: invisible to users, attached to the
# issue, and durable across restarts of the webhook service.
PROCESSED_COMMENTS_PROPERTY = "ltw-answered-comments"
DEFAULT_PROCESSED_LABEL = "ltw-processed"
DEFAULT_AWAITING_LABEL = "ltw-awaiting-input"


def trigger_keyword() -> str:
    """The word that makes this agent act on an issue."""
    return os.environ.get("LTW_TRIGGER_KEYWORD", "").strip() or DEFAULT_TRIGGER_KEYWORD


def processed_label() -> str:
    """Label stamped on an issue once a run has finished with it."""
    return os.environ.get("LTW_PROCESSED_LABEL", "").strip() or DEFAULT_PROCESSED_LABEL


def awaiting_label() -> str:
    """Label stamped when the agent asked a question and is waiting for answers."""
    return os.environ.get("LTW_AWAITING_LABEL", "").strip() or DEFAULT_AWAITING_LABEL


def mentions_trigger(*texts: str) -> bool:
    """True when this agent's trigger keyword appears in any of ``texts``.

    The matching rule is shared; the keyword is this agent's, because only this
    agent reads ``LTW_TRIGGER_KEYWORD``.
    """
    return _mentions_trigger(*texts, keyword=trigger_keyword())


__all__ = [
    "AGENT_FOOTER",
    "AGENT_SIGNATURE",
    "DEFAULT_AWAITING_LABEL",
    "DEFAULT_PROCESSED_LABEL",
    "DEFAULT_TRIGGER_KEYWORD",
    "PROCESSED_COMMENTS_PROPERTY",
    "awaiting_label",
    "is_agent_comment",
    "mentions_trigger",
    "processed_label",
    "trigger_keyword",
]
