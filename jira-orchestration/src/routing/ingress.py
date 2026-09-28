"""Deciding whether a webhook delivery deserves a run at all.

Three gates, in this order, and the order is not arbitrary — each one is cheaper
than the next, and each removes work the following gates would otherwise do:

1. **Mention filter.** No trigger keyword, no run. Most comments on a busy
   project are people talking to each other.
2. **Self-guard.** The system must never answer its own reply. Identity cannot
   decide this — the agent usually posts as the same Jira account as the people
   using it — so the signature does.
3. **Idempotency.** Jira redelivers. The key is `comment.id + webhookEvent`,
   checked *before* classification so a redelivery costs neither a model call
   nor a child workflow.
There is no per-issue lock. One was documented here as gate 4, and a
``lock_key`` was defined and tested, but nothing ever called it — two comments
seconds apart were never serialised. The owner chose to delete it (D5,
2026-09-25): each comment is its own request, a redelivery is caught by gate 3,
Temporal refuses a second workflow with the same id, and the work-breakdown
agent's own duplicate check stops the second of two identical builds.

Gates 1 and 2 are pure and live here. Gate 3 touches shared storage, so it is an
activity; this module only defines what its answer means.

**Ignoring is silent; failing is not.** A comment that fails gate 1 or 2 gets no
reply at all — answering "this wasn't for me" on every unrelated comment would
make the system unbearable in a busy project. But a request the system could
not *read* (a 403, a Jira 500, a network blip) is not "not for me", and must be
answered. Both used to come back as ``{"proceed": False}``, so the router
silently dropped real failures under a comment claiming the drop was
deliberate (D0, 2026-09-25). They are now separate, closed outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from shared.keywords import is_agent_comment, mentions_trigger


class IngressOutcome(str, Enum):
    """What ingress concluded. The router branches on this and nothing else."""

    PROCEED = "proceed"
    IGNORED = "ignored"  # deliberately silent — one of IgnoreReason
    FAILED = "failed"  # something threw; the person must be told


class IgnoreReason(str, Enum):
    """The closed set of reasons to stay silent. Anything else is a failure."""

    NO_KEYWORD = "no trigger keyword in the comment"
    OWN_ACCOUNT = "the comment was written by this system's own account"
    SIGNATURE = "the comment carries this system's signature"
    DUPLICATE = "this comment has already been answered"
    OUTSIDE_PROJECT = "the issue is outside the projects this deployment may answer in"
    # Nothing to reply to: no issue in the event, or an issue with no comments.
    NO_ISSUE = "no issue key in the event"
    NO_COMMENT = "the event named no readable comment"


@dataclass(frozen=True)
class IngressVerdict:
    """Whether to run, and — when not — which closed reason says why."""

    proceed: bool
    ignored: IgnoreReason | None = None

    @property
    def dropped(self) -> bool:
        return not self.proceed

    @property
    def reason(self) -> str:
        return self.ignored.value if self.ignored else ""


PROCEED = IngressVerdict(proceed=True)


def screen_comment(
    body: str,
    *,
    keyword: str = "Aetherion",
    author_account_id: str = "",
    bot_account_id: str = "",
) -> IngressVerdict:
    """Gates 1 and 2: is this comment addressed to us, and did we write it?

    ``bot_account_id`` is checked when configured, because it is cheaper and
    more reliable than a string match. The signature check runs regardless: the
    two agents and the router may post as different accounts, and a reply from
    any of them must never retrigger the system.
    """
    text = body or ""

    if not mentions_trigger(text, keyword=keyword):
        return IngressVerdict(False, IgnoreReason.NO_KEYWORD)

    if bot_account_id and author_account_id and author_account_id == bot_account_id:
        return IngressVerdict(False, IgnoreReason.OWN_ACCOUNT)

    if is_agent_comment(text):
        # The structural guard. Even if the account check is misconfigured, a
        # comment carrying our signature can never start a run.
        return IngressVerdict(False, IgnoreReason.SIGNATURE)

    return PROCEED


def idempotency_key(comment_id: str, webhook_event: str) -> str:
    """What makes two deliveries 'the same request'.

    The event is part of the key because the same comment id arrives for
    ``comment_created`` and ``comment_updated``, and editing a comment to add a
    new instruction is a genuinely new request.
    """
    return f"{(webhook_event or 'unknown').strip()}:{(comment_id or '').strip()}"
