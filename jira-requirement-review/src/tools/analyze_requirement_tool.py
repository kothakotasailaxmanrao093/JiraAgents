"""@tool: LLM analysis of the gathered context into structured findings.

Routes the LLM call through the **Aetherion AI Gateway** (``agent_lib.gateway.ai``).
The gateway holds the provider credentials centrally, so the agent does NOT need
an ``OPENAI_API_KEY`` (or any provider key) of its own. The provider is derived
from the configured model id; the gateway takes provider + model_name separately
and supports a native ``system_prompt``.

Degrades gracefully: on any gateway/parse failure it returns
``{"findings": [], "error": ...}`` rather than raising, so the agent reports
cleanly without fabricating a draft.
"""

from __future__ import annotations

import logging
from typing import Any

from aetherion_sdk import tool
from dotenv import load_dotenv

from config import model_id_from_env, provider_from_model
from review.dedup import dedupe_review
from review.known_questions import (
    drop_invented_topics,
    drop_repeats,
    drop_ungrounded,
    listed_open_questions,
)
from review.llm_response import extract_json
from review.parser import parse_findings, parse_readiness
from review.prompt import SYSTEM_PROMPT, build_user_message

load_dotenv()
logger = logging.getLogger(__name__)

# Generous cap so the findings JSON is never truncated mid-object.
MAX_OUTPUT_TOKENS = 4000


@tool(name="analyze_requirement")
async def analyze_requirement(
    documents: list[dict[str, Any]],
    issue_key: str,
    issue_summary: str | None = None,
    model_id: str | None = None,
    is_subtask: bool = False,
) -> dict[str, Any]:
    if not documents:
        return {"findings": [], "readiness": None, "error": "no_context_documents"}

    try:
        # Aetherion AI Gateway — credentials are managed by the platform gateway.
        from agent_lib.gateway.ai import ai_gateway

        model = model_id_from_env(model_id)
        provider = provider_from_model(model)
        known = listed_open_questions(documents)
        user_message = build_user_message(issue_key, issue_summary, documents, known, is_subtask)

        reply = await ai_gateway.chat(
            provider=provider,
            model_name=model,
            prompt=user_message,
            system_prompt=SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        content = reply.get("content", "") if isinstance(reply, dict) else str(reply)
        raw = extract_json(content)
    except Exception as e:
        logger.error("LLM analysis failed for %s: %s", issue_key, e, exc_info=True)
        return {"findings": [], "readiness": None, "error": str(e)}

    review = dedupe_review(parse_findings(raw))
    findings = [f.model_dump(mode="json") for f in review.findings]
    findings, over_reach = drop_ungrounded(findings, documents)
    findings, invented = drop_invented_topics(findings, documents)
    over_reach += invented
    if over_reach:
        logger.info(
            "analyze_requirement: over-reach — dropped %d finding(s) citing no source read for %s",
            over_reach,
            issue_key,
        )
    kept = drop_repeats(findings, known)
    if len(kept) < len(findings):
        logger.info(
            "analyze_requirement: dropped %d finding(s) repeating listed open questions",
            len(findings) - len(kept),
        )
    findings = kept
    readiness = parse_readiness(raw)
    logger.info(
        "analyze_requirement: %d finding(s) for %s, readiness=%s",
        len(findings),
        issue_key,
        readiness.level.value if readiness else None,
    )
    return {
        "findings": findings,
        "readiness": readiness.model_dump(mode="json") if readiness else None,
        "known_questions": known,
        # Review's own quality metric: findings dropped for citing nothing read.
        "over_reach": over_reach,
        "error": None,
    }
