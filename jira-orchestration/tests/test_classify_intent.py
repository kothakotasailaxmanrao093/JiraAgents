"""``classify_intent`` — the router's one model call (Layer 2).

It had no tests, and it imported ``agent_lib.llm.llm_factory``: a module that is
empty in the installed SDK. Every Layer 2 decision in production fell through
to "the classifier could not be reached", so comments without an explicit
verb were always answered with "Which did you mean?". These tests drive the
real function against a fake gateway client, and pin the import path it uses.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest

from routing import settings
from tools import tools


class _FakeClient:
    calls: list[dict[str, Any]] = []
    reply: Any = {"content": '{"BUILD": 0.1, "REVIEW": 0.8, "QUESTION": 0.05, "CHATTER": 0.05}'}
    raises: Exception | None = None

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def chat(self, **kwargs: Any) -> Any:
        _FakeClient.calls.append(kwargs)
        if _FakeClient.raises:
            raise _FakeClient.raises
        return _FakeClient.reply


@pytest.fixture
def fake_gateway(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.raises = None
    _FakeClient.reply = {
        "content": '{"BUILD": 0.1, "REVIEW": 0.8, "QUESTION": 0.05, "CHATTER": 0.05}'
    }
    module = types.ModuleType("agent_lib.gateway.ai")
    module.AiGatewayClient = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_lib.gateway.ai", module)
    return _FakeClient


def test_the_gateway_client_the_classifier_imports_really_exists() -> None:
    """The regression guard: the module and class must resolve in the real
    installed SDK, not just in a fake."""
    module = importlib.import_module("agent_lib.gateway.ai")
    assert hasattr(module, "AiGatewayClient")


async def test_scores_come_back_from_the_gateway(fake_gateway) -> None:
    result = await tools.classify_intent("is anything missing here?", "Consent capture")
    assert result["scores"] == {"BUILD": 0.1, "REVIEW": 0.8, "QUESTION": 0.05, "CHATTER": 0.05}


async def test_the_call_names_a_provider_and_a_model(fake_gateway, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_CLASSIFIER_MODEL", "gpt-5.1")
    await tools.classify_intent("have a look", "")
    call = fake_gateway.calls[0]
    assert call["model_name"] == "gpt-5.1"
    assert call["provider"] == "openai"
    assert "Comment: have a look" in call["prompt"]


async def test_a_gateway_failure_means_ask_and_names_the_real_error(fake_gateway) -> None:
    fake_gateway.raises = TimeoutError("gateway timed out")
    result = await tools.classify_intent("have a look", "")
    assert result["scores"] is None
    assert "TimeoutError" in result["error"]
    assert "gateway timed out" in result["error"]


async def test_prose_with_no_json_is_reported_as_unparseable(fake_gateway) -> None:
    fake_gateway.reply = {"content": "I think this is a review request."}
    result = await tools.classify_intent("have a look", "")
    assert result["scores"] is None
    assert result["error"] == "unparseable classifier response"


@pytest.mark.parametrize(
    "model, provider",
    [
        ("gpt-5.1", "openai"),
        ("gpt-4o", "openai"),
        ("claude-sonnet-5", "anthropic"),
        ("gemini-2.5-pro", "gemini"),
    ],
)
def test_the_provider_is_inferred_from_the_model(model: str, provider: str, monkeypatch) -> None:
    monkeypatch.delenv("ORCH_CLASSIFIER_PROVIDER", raising=False)
    assert settings.provider_for_model(model) == provider


def test_an_explicit_provider_wins(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_CLASSIFIER_PROVIDER", "bedrock")
    assert settings.provider_for_model("gpt-5.1") == "bedrock"
