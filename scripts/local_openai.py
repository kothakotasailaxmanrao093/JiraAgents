"""Give Planning and Coding a model on a laptop (2026-10-26; Coding's gateway 2026-11-25).

The platform refuses an agent whose own code builds an OpenAI client (it would
bypass the AI gateway), so on a laptop — which has no gateway — this launcher
supplies one. Never imported by the agents themselves; only
scripts/local_worker.py calls install().

- Planning asks src/ingestion/local_client.py for a client: given OpenAI's.
- Coding since 0.0.14 (2026-11-24) calls the AI gateway only
  (src/coding/llm.py ``_client()`` -> AiGatewayClient). Here ``_client`` is
  replaced by LocalGateway: the same two calls (chat, chat_with_tools) and
  the same reply shapes, answered by OpenAI with OPENAI_API_KEY.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

_CLIENT_MODULES = {
    "planning-agent": "src.ingestion.local_client",
    "coding-agent": "src.coding.local_client",  # Coding up to 0.0.13
}


class LocalGateway:
    """AiGatewayClient's interface, answered by OpenAI (a laptop run only)."""

    async def __aenter__(self) -> LocalGateway:
        from openai import AsyncOpenAI

        self._openai = AsyncOpenAI()  # OPENAI_API_KEY from .env.local
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._openai.close()

    @staticmethod
    def _model(model_name: str) -> str:
        # A Claude or Gemini name means nothing to OpenAI: use the laptop's model.
        if model_name.startswith(("gpt-", "o1", "o3", "o4")):
            return model_name
        return os.environ.get("OPENAI_MODEL", "").strip() or "gpt-6.2-sol"

    async def chat(self, *, provider: str, model_name: str, prompt: str,
                   system_prompt: str = "", max_tokens: int = 16000, **_: Any) -> dict:
        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else [])
        messages.append({"role": "user", "content": prompt})
        reply = await self._openai.chat.completions.create(
            model=self._model(model_name), messages=messages, max_completion_tokens=max_tokens
        )
        out = reply.model_dump()
        return {"content": reply.choices[0].message.content or "", "usage": out.get("usage")}

    async def chat_with_tools(self, *, provider: str, model_name: str,
                              messages: list[dict], tools: list[dict], **_: Any) -> dict:
        """One tool turn. OpenAI's chat endpoint refuses tools with reasoning on
        its newest models, so this goes through the Responses API, thinking
        on, and the answer is given back in the chat shape the gateway relays."""
        effort = os.environ.get("OPENAI_REASONING_EFFORT", "").strip() or "high"
        reply = await self._openai.responses.create(
            model=self._model(model_name),
            input=_to_input(messages),
            tools=[_to_tool(t) for t in tools],
            reasoning={"effort": effort},
        )
        return _to_chat(reply)


def _to_tool(tool: dict) -> dict:
    """A chat-format function tool as the Responses API takes it."""
    fn = tool.get("function") or tool
    out = {"type": "function", "name": fn["name"], "parameters": fn.get("parameters") or {}}
    if fn.get("description"):
        out["description"] = fn["description"]
    return out


def _to_input(messages: list[dict]) -> list[dict]:
    """Chat messages (with assistant tool calls and tool results) as Responses input."""
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""),
                          "output": str(m.get("content") or "")})
            continue
        content = m.get("content")
        if isinstance(content, list):  # parts: keep their text
            content = "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        if content:
            items.append({"role": role, "content": str(content)})
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            items.append({"type": "function_call", "call_id": call.get("id", ""),
                          "name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"})
    return items


def _to_chat(reply: Any) -> dict:
    """A Responses answer in the chat shape: choices[0].message with its tool calls."""
    text, calls = [], []
    for item in reply.output or []:
        if item.type == "message":
            text += [c.text for c in item.content or [] if getattr(c, "text", None)]
        elif item.type == "function_call":
            calls.append({"id": item.call_id, "type": "function",
                          "function": {"name": item.name, "arguments": item.arguments}})
    incomplete = getattr(reply, "incomplete_details", None)
    finish = "length" if incomplete and getattr(incomplete, "reason", "") == "max_output_tokens" else (
        "tool_calls" if calls else "stop")
    usage = reply.usage
    return {
        "choices": [{"finish_reason": finish,
                     "message": {"content": "\n".join(text), "tool_calls": calls}}],
        "usage": {
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "prompt_tokens_details": {"cached_tokens": getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0},
            "completion_tokens_details": {"reasoning_tokens": getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0},
        },
    }


def install(agent: str) -> bool:
    """Give this agent its laptop model client; False when not needed."""
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    if agent == "coding-agent":
        try:
            llm = importlib.import_module("src.coding.llm")
        except ModuleNotFoundError:
            llm = None
        if llm is not None and hasattr(llm, "_client") and not _has_module(_CLIENT_MODULES[agent]):
            llm._client = LocalGateway
            return True
    module = _CLIENT_MODULES.get(agent)
    if not module:
        return False
    try:
        hook = importlib.import_module(module)
    except ModuleNotFoundError:
        return False
    from openai import AsyncOpenAI

    hook.install(AsyncOpenAI)
    return True


def _has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except ModuleNotFoundError:
        return False
