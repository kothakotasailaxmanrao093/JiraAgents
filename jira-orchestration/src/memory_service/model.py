"""One model call that answers in JSON, for the memory's two small
judgements: which question a comment answers, and how much a card touches
each service. The same route as the router's classifier: the Aetherion AI
Gateway, or OpenAI directly on a laptop run."""

from __future__ import annotations

import json
import re
from typing import Any

from routing import settings
from shared import llm

MAX_TOKENS = 2_000
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model's answer, tolerating prose or fences around it."""
    found = _OBJECT.search(text or "")
    if not found:
        raise ValueError("the model returned no JSON object")
    value = json.loads(found.group(0))
    if not isinstance(value, dict):
        raise ValueError("the model returned JSON that is not an object")
    return value


async def ask_json(system_prompt: str, prompt: str) -> dict[str, Any]:
    """The model's JSON object; raises when the model cannot be reached."""
    model = settings.classifier_model()
    provider = settings.provider_for_model(model)
    if llm.direct_openai():  # a laptop run: no gateway there
        text = await llm.openai_chat(
            provider=provider,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=MAX_TOKENS,
        )
    else:
        from agent_lib.gateway.ai import AiGatewayClient  # activity-side only

        async with AiGatewayClient() as gateway:
            reply = await gateway.chat(
                provider=provider,
                model_name=model,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=MAX_TOKENS,
            )
        text = (reply or {}).get("content") or ""
    return parse_json(text)
