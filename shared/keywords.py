"""Trigger vocabulary and agent identity. Pure — safe to import from workflow code.

Nothing here touches the network, so the ingest side can ask what the trigger
word is without pulling in an HTTP client and the whole REST layer.

Two things live here rather than in any one agent, because the system must speak
with **one voice**: the signature a reply is signed with, and the rule for
deciding whether a comment is a fresh request or something the system itself
wrote. If each agent owned its own copy, a fourth agent could sign differently
and a human would have no reliable way to recognise an automated reply.

What stays with each agent is what differs per deployment: the label it stamps
and the issue property it keeps its own bookkeeping in.
"""

from __future__ import annotations

import re

DEFAULT_TRIGGER_KEYWORD = "Aetherion"

# How the system signs its own comments.
#
# Deliberately NOT "@Aetherion": the trigger matcher looks for "Aetherion" on
# word boundaries, so a signature like "@no-reply-aetherion" or
# "Aetherion (automated)" still reads as a mention and the system answers its own
# reply forever. Gluing a letter onto the keyword — AetherionAgent — makes the
# signature structurally incapable of matching, so the loop cannot form even if
# the signature check itself were removed.
#
# This is the property Phase 4 depends on: the router posts every reply, and the
# footer it appends must never come back as a fresh request.
AGENT_SIGNATURE = "AetherionAgent"
AGENT_FOOTER = f"— {AGENT_SIGNATURE} · automated reply"


def mentions_trigger(*texts: str, keyword: str = DEFAULT_TRIGGER_KEYWORD) -> bool:
    """True when the trigger keyword appears in any of ``texts``.

    Whole-word and case-insensitive, so "aetherion" fires but "aetherionx"
    does not — a substring match would trip on unrelated wording.

    ``keyword`` is passed in rather than read from the environment here, because
    each agent names its own override variable and this module must stay pure.
    """
    pattern = re.compile(rf"(?<![\w]){re.escape(keyword)}(?![\w])", re.IGNORECASE)
    return any(pattern.search(text or "") for text in texts)


def is_agent_comment(body: str) -> bool:
    """True when a comment was written by this system.

    Identity cannot answer this: an agent usually runs as the same Jira account
    as the people using it, so "who wrote it" is the same for both. The
    signature can, and it is a string no person would type by accident.
    """
    return AGENT_SIGNATURE in (body or "")
