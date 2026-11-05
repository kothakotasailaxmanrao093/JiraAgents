"""Secrets from wherever the platform provides them (2026-10-15)."""

from shared.settings import secret


def test_a_mounted_file_wins_over_the_variable(tmp_path, monkeypatch):
    (tmp_path / "GITHUB_TOKEN").write_text("from-the-store\n")
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "from-dot-env")
    assert secret("GITHUB_TOKEN") == "from-the-store"


def test_a_file_named_by_NAME_FILE_wins_over_everything(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("pointed-at")
    (tmp_path / "GITHUB_TOKEN").write_text("in-the-folder")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(tmp_path / "a"))
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    assert secret("GITHUB_TOKEN") == "pointed-at"


def test_without_a_file_the_variable_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "  from-dot-env  ")
    assert secret("GITHUB_TOKEN") == "from-dot-env"


def test_nothing_anywhere_gives_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    assert secret("NOPE_TOKEN") == "" and secret("NOPE_TOKEN", "d") == "d"


def test_an_empty_file_does_not_hide_the_variable(tmp_path, monkeypatch):
    (tmp_path / "GITHUB_TOKEN").write_text("\n")
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "from-dot-env")
    assert secret("GITHUB_TOKEN") == "from-dot-env"


def test_publishing_names_the_credentials_in_dot_env_never_their_values(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import publish

    (tmp_path / ".env").write_text(
        "JIRA_API_TOKEN=abc\nGITHUB_TOKEN=\nORCH_ALLOWED_PROJECT_KEYS=FL,BTS\n"
        "JIRA_PROJECT_KEY=FL\nexport NEO4J_PASSWORD='p'\n# GMAIL_APP_PASSWORD=x\n"
        "AGENTS_GATEWAY_KEY=k\n"
    )
    assert publish.secrets_in_env(tmp_path) == [
        "JIRA_API_TOKEN",
        "NEO4J_PASSWORD",
        "AGENTS_GATEWAY_KEY",
    ], "empty, commented and identifier-like names are not credentials"
