"""Jira-backed context sources: target issue, parent card, and subtasks.

Each issue contributes up to two documents — its description and its comments —
as separate labeled documents so the LLM can cite precise evidence.
"""

from __future__ import annotations

import logging

from .base import ContextDocument, ContextSource, FetchContext

logger = logging.getLogger(__name__)


def _issue_documents(
    bundle: dict, role_label: str, *, is_target: bool, role: str = "parent"
) -> list[ContextDocument]:
    """Build description/comments documents for one issue bundle.

    ``role`` ("target" / "parent" / "subtask") selects the document ``kind``,
    which is what :mod:`context.budget` ranks on. It is carried explicitly
    rather than inferred from ``role_label`` because the label is prose: a
    target's linked-issues document also starts with "Target", and ranking on
    that prefix once put linked issues below subtask comments.
    """
    docs: list[ContextDocument] = []
    key = bundle.get("key")

    description = (bundle.get("description_text") or "").strip()
    if description:
        docs.append(
            ContextDocument(
                source_id=f"{key}:description",
                source_label=f"{role_label} (description)",
                kind="requirement" if is_target else f"{role}_context",
                text=description,
                metadata={"issue_key": key},
            )
        )

    comments = bundle.get("comments") or []
    rendered = "\n".join(
        f"- {c.get('author') or 'Unknown'}: {c.get('body', '')}" for c in comments if c.get("body")
    ).strip()
    if rendered:
        docs.append(
            ContextDocument(
                source_id=f"{key}:comments",
                source_label=f"{role_label} (comments)",
                kind=f"{role}_comments",
                text=rendered,
                metadata={"issue_key": key, "count": len(comments)},
            )
        )
    return docs


class TargetIssueSource(ContextSource):
    source_id = "target_issue"

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        bundle = ctx.target_bundle or {}
        key = bundle.get("key", ctx.issue_key)
        return _issue_documents(bundle, f"Target {key}", is_target=True, role="target")


class ParentIssueSource(ContextSource):
    source_id = "parent_issue"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return ctx.include_parent and bool((ctx.target_bundle or {}).get("parent_key"))

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        parent_key = (ctx.target_bundle or {}).get("parent_key")
        try:
            parent = await ctx.jira.get_parent_bundle(parent_key)
        except Exception as e:
            ctx.warnings.append(f"parent {parent_key} fetch failed: {e}")
            logger.warning("Parent %s fetch failed: %s", parent_key, e)
            return []
        if not parent:
            return []
        return _issue_documents(
            parent, f"Parent {parent.get('key')}", is_target=False, role="parent"
        )


class SubtasksSource(ContextSource):
    source_id = "subtasks"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return ctx.include_subtasks and bool((ctx.target_bundle or {}).get("subtask_keys"))

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        keys = (ctx.target_bundle or {}).get("subtask_keys") or []
        try:
            bundles = await ctx.jira.get_subtask_bundles(keys)
        except Exception as e:
            ctx.warnings.append(f"subtasks fetch failed: {e}")
            logger.warning("Subtasks fetch failed: %s", e)
            return []
        docs: list[ContextDocument] = []
        for bundle in bundles:
            docs.extend(
                _issue_documents(
                    bundle, f"Subtask {bundle.get('key')}", is_target=False, role="subtask"
                )
            )
        return docs


class LinkedIssuesSource(ContextSource):
    """Surfaces the target issue's linked issues (blocks/relates-to/etc.) as a
    single evidence document, so the LLM can ground Dependency Question findings
    in a concrete linked ticket rather than an implied/unverifiable dependency."""

    source_id = "linked_issues"

    def is_enabled(self, ctx: FetchContext) -> bool:
        return ctx.include_linked_issues and bool((ctx.target_bundle or {}).get("linked_issues"))

    async def load(self, ctx: FetchContext) -> list[ContextDocument]:
        bundle = ctx.target_bundle or {}
        linked = bundle.get("linked_issues") or []
        if not linked:
            return []
        key = bundle.get("key", ctx.issue_key)
        rendered = "\n".join(
            f"- {li.get('key')} [{li.get('link_type') or 'relates to'}] "
            f"({li.get('direction', '')}): {li.get('summary') or '(no summary)'}"
            for li in linked
            if li.get("key")
        ).strip()
        if not rendered:
            return []
        return [
            ContextDocument(
                source_id=f"{key}:linked_issues",
                source_label=f"Target {key} (linked issues)",
                kind="linked_issues",
                text=rendered,
                metadata={"issue_key": key, "count": len(linked)},
            )
        ]
