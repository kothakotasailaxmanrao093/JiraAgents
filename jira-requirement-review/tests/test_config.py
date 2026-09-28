"""Provider detection + model-id resolution for the AI Gateway."""

from __future__ import annotations

from config import model_id_from_env, provider_from_model


def test_provider_from_model_prefixes():
    assert provider_from_model("gpt-4o") == "openai"
    assert provider_from_model("gpt-5.4") == "openai"
    assert provider_from_model("o3-mini") == "openai"
    assert provider_from_model("claude-sonnet-4-6") == "anthropic"
    assert provider_from_model("gemini-2.0-pro") == "gemini"
    assert provider_from_model("anthropic.claude-v2") == "bedrock"  # bedrock, not anthropic
    assert provider_from_model("amazon.titan-text") == "bedrock"


def test_provider_explicit_override_wins():
    assert provider_from_model("gpt-4o", override="anthropic") == "anthropic"


def test_provider_env_override(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert provider_from_model("gpt-4o") == "gemini"


def test_provider_defaults_to_openai_for_unknown():
    assert provider_from_model("some-weird-model") == "openai"


def test_model_id_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_ID", "claude-sonnet-4-6")
    assert model_id_from_env() == "claude-sonnet-4-6"
    assert model_id_from_env("gpt-5.1") == "gpt-5.1"  # explicit override wins


def test_llm_model_id_takes_precedence(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL_ID", "gpt-5.1")
    monkeypatch.setenv("LLM_MODEL_ID", "claude-sonnet-4-6")
    assert model_id_from_env() == "claude-sonnet-4-6"


def test_model_id_default(monkeypatch):
    from config import DEFAULT_MODEL_ID

    monkeypatch.delenv("OPENAI_MODEL_ID", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    assert model_id_from_env() == DEFAULT_MODEL_ID == "gpt-5.1"
