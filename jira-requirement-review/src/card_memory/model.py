"""One model call that answers in JSON, for the memory's two small judgements:
which question a comment answers, and how much a card touches each service.

The same route as the review itself (analyze_requirement_tool): the Aetherion
AI Gateway, or OpenAI directly on a laptop run.
"""

from __future__ import annotations

from typing import Any

from config import model_id_from_env, provider_from_model
from review.llm_response import extract_json
from shared import llm

MAX_TOKENS = 2_000


async def ask_json(system_prompt: str, prompt: str) -> dict[str, Any]:
    """The model's JSON object; raises when the model cannot be reached."""
    model = model_id_from_env(None)
    provider = provider_from_model(model)
    if llm.direct_openai():
        content = await llm.openai_chat(
            provider=provider,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=MAX_TOKENS,
        )
    else:
        from agent_lib.gateway.ai import ai_gateway

        reply = await ai_gateway.chat(
            provider=provider,
            model_name=model,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=MAX_TOKENS,
        )
        content = reply.get("content", "") if isinstance(reply, dict) else str(reply)
    found = extract_json(content)
    return found if isinstance(found, dict) else {}
