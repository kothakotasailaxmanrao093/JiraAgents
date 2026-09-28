"""The comment that asked for the review.

Only present on the webhook path, where a person wrote "@Aetherion review this,
particularly the error handling" — which says what they actually want looked at.
Without it the agent reviews the ticket in general and answers a question nobody
asked.

It is a separate source rather than part of the target issue because it is
optional, differently ranked, and arrives by a different route: the router
passes the ``comment_id`` it received from Jira, and the comment is picked out of
the bundle already fetched rather than re-read over the network.
"""

from __future__ import annotations

import logging

from .base import ContextDocument, ContextSource, FetchContext

logger = logging.getLogger(__name__)


class TriggerCommentSource(ContextSource):
    """The specific comment that triggered this run, when there was one."""

    source_id = "trigger_comment"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return bool(ctx.trigger_comment_id)

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        bundle = ctx.target_bundle or {}
        key = bundle.get("key", ctx.issue_key)
        wanted = str(ctx.trigger_comment_id or "").strip()

        for comment in bundle.get("comments") or []:
            if str(comment.get("id") or "") != wanted:
                continue
            body = (comment.get("body") or "").strip()
            if not body:
                break
            return [
                ContextDocument(
                    source_id=f"{key}:trigger_comment:{wanted}",
                    source_label=f"The comment that asked for this review (on {key})",
                    kind="trigger_comment",
                    text=body,
                    metadata={
                        "issue_key": key,
                        "comment_id": wanted,
                        "author": comment.get("author"),
                    },
                )
            ]

        # Not fatal: the review still runs, it just answers the ticket in general.
        # Said out loud because "I ignored your instruction" should never be silent.
        ctx.warnings.append(
            f"The triggering comment ({wanted}) could not be found on {key}, so the "
            "review covers the ticket as a whole rather than what the comment asked for."
        )
        logger.warning("Trigger comment %s not found on %s", wanted, key)
        return []
