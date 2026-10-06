"""scripts/local_worker.py: all four agents on one laptop (2026-10-03).

``aetherion run`` puts every local worker on one shared queue, so a job could
reach an agent that does not have it. The launcher gives each agent folder its
own queue, maps every agent name to it, and points the router's child-queue
settings at the same queues.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location(
        "local_worker", ROOT / "scripts" / "local_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_agent_folder_has_a_queue_of_its_own(launcher) -> None:
    queues = [launcher.local_queue(folder) for folder in launcher.AGENTS]
    assert len(set(queues)) == len(queues) == 4


def test_every_agent_name_reaches_its_own_folders_queue(launcher) -> None:
    mapping = launcher.local_agent_map()
    for folder, names in launcher.AGENT_NAMES.items():
        for name in names:
            assert mapping[name] == launcher.local_queue(folder)
    assert {
        "JiraOrchestration",
        "JiraTaskCreation",
        "JiraRequirementReview",
        "planning_agent",
    } <= set(mapping)


def test_the_router_is_pointed_at_the_settings_it_reads(launcher) -> None:
    settings = (ROOT / "jira-orchestration" / "src" / "routing" / "settings.py").read_text()
    for folder, name in launcher.CHILD_QUEUE_SETTINGS.items():
        assert f"export {name}={launcher.local_queue(folder)}" in launcher.exports()
        if folder != "planning-agent":  # read by the GitHub/Planning routing once built
            assert name in settings, name
