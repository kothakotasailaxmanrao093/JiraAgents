"""scripts/local_infra.py: Neo4j and storage from Docker, for a laptop run (2026-10-08).

Planning writes the design to storage, Coding reads it back and the router
attaches it to the ticket — so all three must see the same storage, and
Planning and Coding the same graph. Nothing is written to a file, and a value
already set (the shell, the agent's own .env) is never replaced.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NEO4J = {"NEO4J_AUTH": "neo4j/graph-pass", "NEO4J_PLUGINS": "[]"}
S3 = {"RUSTFS_ACCESS_KEY": "ak", "RUSTFS_SECRET_KEY": "sk"}
NAMES = (
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "APP_ENV",
    "STORAGE_ENDPOINT",
    "STORAGE_SECURE",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "TENANT_ID",
    "LOCAL_STORAGE_BUCKET",
)


@pytest.fixture
def infra(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "local_infra", ROOT / "scripts" / "local_infra.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    containers = {module.NEO4J_CONTAINER: NEO4J, module.STORAGE_CONTAINER: S3}
    monkeypatch.setattr(module, "container_env", lambda name: containers.get(name, {}))
    return module


def test_the_graph_and_the_storage_come_from_the_containers(infra) -> None:
    values = infra.settings(NEO4J, S3)
    assert values["NEO4J_URI"] == "bolt://localhost:7687"
    assert (values["NEO4J_USER"], values["NEO4J_PASSWORD"]) == ("neo4j", "graph-pass")
    assert values["APP_ENV"] == "local" and values["STORAGE_ENDPOINT"] == "http://localhost:9000"
    assert (values["STORAGE_ACCESS_KEY"], values["STORAGE_SECRET_KEY"]) == ("ak", "sk")
    assert values["TENANT_ID"] == "planning-local"


def test_every_agent_shares_the_storage(infra, monkeypatch) -> None:
    import os

    for agent in (
        "planning-agent",
        "coding-agent",
        "jira-orchestration",
        "jira-task-creation",
        "jira-requirement-review",
    ):
        for name in NAMES:
            monkeypatch.delenv(name, raising=False)
        assert "STORAGE_ACCESS_KEY" in infra.install(agent)
        assert os.environ["TENANT_ID"] == "planning-local"


@pytest.mark.parametrize(
    "agent", ["jira-orchestration", "jira-task-creation", "jira-requirement-review"]
)
def test_only_planning_and_coding_get_the_graph(infra, agent) -> None:
    import os

    infra.install(agent)
    assert "NEO4J_PASSWORD" not in os.environ


def test_a_value_already_set_wins(infra, monkeypatch) -> None:
    import os

    monkeypatch.setenv("NEO4J_URI", "neo4j+s://hosted.example:7687")
    monkeypatch.setenv("TENANT_ID", "my-bucket")
    filled = infra.install("planning-agent")
    assert "NEO4J_URI" not in filled and "TENANT_ID" not in filled
    assert os.environ["NEO4J_URI"] == "neo4j+s://hosted.example:7687"
    assert os.environ["TENANT_ID"] == "my-bucket"


def test_no_docker_fills_nothing(infra, monkeypatch) -> None:
    monkeypatch.setattr(infra, "container_env", lambda name: {})
    assert infra.install("planning-agent") == []


def test_nothing_is_written_to_a_file() -> None:
    source = (ROOT / "scripts" / "local_infra.py").read_text()
    assert "open(" not in source and "write_text" not in source
