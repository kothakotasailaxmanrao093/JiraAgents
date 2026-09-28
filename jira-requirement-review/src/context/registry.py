"""The single place that declares which context sources run, and in what order.

To add a new source (Confluence, Slack, linked issues, ...), implement
``ContextSource`` and append it here — nothing else changes.
"""

from __future__ import annotations

from .attachment_source import AttachmentsSource, ConfluenceSource
from .base import ContextSource, FetchContext
from .jira_sources import (
    LinkedIssuesSource,
    ParentIssueSource,
    SubtasksSource,
    TargetIssueSource,
)
from .transcript_source import TranscriptSource
from .trigger_comment_source import TriggerCommentSource


def build_sources(ctx: FetchContext) -> list[ContextSource]:
    return [
        # First: what the person actually asked for, when they asked by comment.
        TriggerCommentSource(),
        TargetIssueSource(),
        ParentIssueSource(),
        SubtasksSource(),
        LinkedIssuesSource(),
        # The requirement is frequently only inside one of these two.
        AttachmentsSource(),
        ConfluenceSource(),
        TranscriptSource(),
    ]
