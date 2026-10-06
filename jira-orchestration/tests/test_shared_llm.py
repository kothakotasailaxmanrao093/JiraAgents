"""A laptop run asks OpenAI directly; Aetherion keeps the gateway (2026-10-03).

The AI Gateway exists only inside Aetherion. A worker started on a laptop with
OPENAI_API_KEY (exported by scripts/run_local.sh from the git-ignored
.env.local) sends the same request to OpenAI; with a gateway configured, or no
key, nothing changes.
"""

from __future__ import annotations

import json

import httpx
import pytest

from shared import llm


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("OPENAI_API_KEY", "AETHERION_LOCAL_RUN", "LOCAL_OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"AETHERION_LOCAL_RUN": "1", "OPENAI_API_KEY": "sk-test"}, True),
        # On Aetherion the launcher's flag is never set: a key the platform may
        # have in its environment must not switch a published agent off the gateway.
        ({"OPENAI_API_KEY": "sk-test"}, False),
        ({"AETHERION_LOCAL_RUN": "0", "OPENAI_API_KEY": "sk-test"}, False),
        ({"AETHERION_LOCAL_RUN": "1", "OPENAI_API_KEY": "  "}, False),
        ({"AETHERION_LOCAL_RUN": "1"}, False),
        ({}, False),
    ],
)
def test_openai_is_used_only_in_a_local_launcher_run_with_a_key(monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert llm.direct_openai() is expected


def test_the_request_matches_what_the_gateway_would_get():
    body = llm.openai_request(
        provider="openai",
        model="gpt-4o",
        prompt="Q",
        system_prompt="S",
        temperature=0.2,
        max_tokens=900,
    )
    assert body == {
        "model": "gpt-4o",
        "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}],
        "temperature": 0.2,
        "max_completion_tokens": 900,
    }


def test_reasoning_models_keep_their_temperature_and_room_to_think():
    body = llm.openai_request(
        provider="openai", model="gpt-5-mini", prompt="Q", temperature=0.0, max_tokens=200
    )
    assert "temperature" not in body
    assert body["max_completion_tokens"] == llm.REASONING_MIN_TOKENS
    assert body["messages"] == [{"role": "user", "content": "Q"}]


def test_another_providers_model_is_answered_by_the_local_default(monkeypatch):
    body = llm.openai_request(provider="anthropic", model="claude-sonnet-4-6", prompt="Q")
    assert body["model"] == llm.DEFAULT_LOCAL_MODEL
    monkeypatch.setenv("LOCAL_OPENAI_MODEL", "gpt-4.1")
    assert llm.openai_request(provider="anthropic", model="x", prompt="Q")["model"] == "gpt-4.1"


async def test_the_reply_text_comes_back(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "BUILD 0.9"}}]})

    reply = await llm.openai_chat(
        transport=httpx.MockTransport(answer), provider="openai", model="gpt-4o", prompt="Q"
    )
    assert reply == "BUILD 0.9"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "gpt-4o"


async def test_an_http_error_is_raised_like_the_gateways(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-bad")
    transport = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(httpx.HTTPStatusError):
        await llm.openai_chat(transport=transport, provider="openai", model="gpt-4o", prompt="Q")
