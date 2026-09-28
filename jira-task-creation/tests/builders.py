"""Raw Jira API payloads, shaped exactly as Jira Cloud returns them."""

from __future__ import annotations

from typing import Any


def adf(text: str) -> dict[str, Any]:
    """Minimal ADF document carrying one paragraph."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def issue_payload(
    *,
    key: str = "KS-12",
    summary: str = "Notification preferences",
    description: str = "This ticket tracks how we notify people.",
    trigger: str | None = "@Aetherion Send email and SMS notifications.",
    labels: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    worklogs: list[dict[str, Any]] | None = None,
    issuelinks: list[dict[str, Any]] | None = None,
    parent: dict[str, Any] | None = None,
    subtasks: list[dict[str, Any]] | None = None,
    changelog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    comments = list(comments or [])
    if trigger:
        # A request is made by commenting, so every fixture carries one unless
        # a test deliberately asks for an issue with no request on it.
        comments.append(comment(trigger, author="Priya", cid="9001"))
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": adf(description) if description else None,
            "issuetype": {"name": "Task"},
            "status": {"name": "To Do"},
            "reporter": {"displayName": "Priya"},
            "created": "2026-09-01T10:00:00.000+0530",
            "labels": labels or [],
            "project": {"key": "KS", "name": "Knowledge Suite"},
            "attachment": attachments or [],
            "comment": {"comments": comments},
            "worklog": {"worklogs": worklogs or []},
            "issuelinks": issuelinks or [],
            "parent": parent,
            "subtasks": subtasks or [],
        },
        "changelog": changelog or {"histories": []},
    }


def comment(text: str, author: str = "Ann", cid: str = "1") -> dict[str, Any]:
    return {
        "id": cid,
        "author": {"displayName": author},
        "created": "2026-09-02T09:30:00.000+0530",
        "body": adf(text),
    }


def attachment(
    filename: str, url: str = "https://example.atlassian.net/attach/1"
) -> dict[str, Any]:
    return {"filename": filename, "mimeType": "text/csv", "content": url, "size": 12}


def link(key: str, relationship: str = "blocks", summary: str = "Email service") -> dict[str, Any]:
    return {
        "type": {"name": "Blocks", "outward": relationship, "inward": "is blocked by"},
        "outwardIssue": {
            "key": key,
            "fields": {
                "summary": summary,
                "issuetype": {"name": "Story"},
                "status": {"name": "Done"},
            },
        },
    }


def changelog(
    field: str = "status", old: str = "To Do", new: str = "In Progress"
) -> dict[str, Any]:
    return {
        "histories": [
            {
                "created": "2026-09-03T11:00:00.000+0530",
                "author": {"displayName": "Ravi"},
                "items": [{"field": field, "fromString": old, "toString": new}],
            }
        ]
    }


def worklog(time_spent: str = "2h", author: str = "Ravi") -> dict[str, Any]:
    return {
        "author": {"displayName": author},
        "started": "2026-09-03T11:00:00.000+0530",
        "timeSpent": time_spent,
        "comment": adf("Investigated the SMS provider."),
    }
