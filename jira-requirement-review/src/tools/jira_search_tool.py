"""@tool: resolve a JQL/Sprint query to a bounded list of issue keys for batch review.

Thin activity shim, same shape as the other Jira tools in this package: all I/O
happens here, never in the deterministic workflow. Best-effort — degrades to an
``error`` string rather than raising, matching ``gather_context``.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import tool
from dotenv import load_dotenv

from config import MAX_JQL_RESULTS
from jira.client import jira_client_from_env

load_dotenv()
logger = logging.getLogger(__name__)


@tool(name="jira_search")
async def jira_search(jql: str, max_results: int | None = None) -> dict[str, Any]:
    jql = (jql or "").strip()
    if not jql:
        return {"issue_keys": [], "total": 0, "error": "jql is required"}

    cap = max_results or MAX_JQL_RESULTS
    logger.info("jira_search: jql=%r max_results=%d", jql, cap)

    try:
        async with jira_client_from_env() as jira:
            await jira.preflight()
            keys = await jira.search_jql(jql, cap)
    except Exception as e:
        logger.error("jira_search failed for %r: %s", jql, e, exc_info=True)
        return {"issue_keys": [], "total": 0, "error": str(e)}

    logger.info("jira_search: %d issue(s) matched jql=%r", len(keys), jql)
    return {"issue_keys": keys, "total": len(keys), "error": None}
