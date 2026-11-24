"""Reading Jira: one card, and the project's recent cards. Read only."""

from __future__ import annotations

from typing import Any

import httpx

from . import config, documents

_CARD_FIELDS = "summary,status,issuetype,priority,assignee,reporter,description,labels,updated,parent"


class NotFound(Exception):
    pass


class JiraError(Exception):
    pass


async def card(issue_key: str) -> dict[str, Any]:
    base, auth = config.jira()
    key = issue_key.strip().upper()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{base}/rest/api/3/issue/{key}", params={"fields": _CARD_FIELDS}, auth=auth
        )
    if r.status_code == 404:
        raise NotFound(f"{key} was not found in Jira")
    if r.status_code >= 400:
        raise JiraError(f"Jira answered {r.status_code}")
    f = r.json().get("fields") or {}
    from shared.adf import to_text  # Orchestration's: Jira's document format as text

    return {
        "key": key,
        "url": f"{base}/browse/{key}",
        "summary": f.get("summary") or "",
        "type": (f.get("issuetype") or {}).get("name") or "",
        "status": (f.get("status") or {}).get("name") or "",
        "priority": (f.get("priority") or {}).get("name") or "",
        "assignee": (f.get("assignee") or {}).get("displayName") or "Unassigned",
        "reporter": (f.get("reporter") or {}).get("displayName") or "",
        "labels": f.get("labels") or [],
        "updated": f.get("updated") or "",
        "parent": (f.get("parent") or {}).get("key") or "",
        "description": (to_text(f["description"]) if f.get("description") else "")[:4000],
        "documents": documents.for_card(key),
    }


async def recent(project: str = "") -> list[dict[str, Any]]:
    base, auth = config.jira()
    project = (project or config.project()).upper()
    jql = (f"project={project} " if project else "") + "ORDER BY updated DESC"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{base}/rest/api/3/search/jql",
            auth=auth,
            params={"jql": jql, "maxResults": 30, "fields": "summary,status,issuetype"},
        )
    if r.status_code >= 400:
        return []
    return [
        {
            "key": i["key"],
            "summary": i["fields"].get("summary") or "",
            "type": (i["fields"].get("issuetype") or {}).get("name") or "",
            "status": (i["fields"].get("status") or {}).get("name") or "",
        }
        for i in r.json().get("issues") or []
    ]
