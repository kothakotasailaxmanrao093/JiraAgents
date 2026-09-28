"""The agent runs inside a Temporal workflow, which may not read the environment.

Found in wf.sbox, never locally, because only the real sandbox enforces it:

    RestrictedWorkflowAccessError: Cannot access os.environ.get from inside a
    workflow.
      agent.py:428   is_question_comment(trigger_body)
      ingest.py:405    strip_trigger_mentions(body, trigger_keyword())
      keywords.py:39     os.environ.get("LTW_TRIGGER_KEYWORD", ...)

Anything needing configuration has to be decided in an **activity** — a @tool —
and handed to the agent as data. These tests fail the moment the agent module
can reach a module that reads the environment, whether or not the call happens.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import sys

# Reading the environment is fine here: these run in activities, not the workflow.
ENV_READING_PACKAGES = (
    "src.jira",
    "src.context",
    "src.tools",
    "src.notifications",
    "src.confluence",
    "src.config",
    "src.classification",
)


def _imported_by_the_workflow() -> set[str]:
    for name in [m for m in sys.modules if m.startswith("src.")]:
        del sys.modules[name]
    before = set(sys.modules)
    importlib.import_module("src.agent.agent")
    return {m for m in set(sys.modules) - before if m.startswith("src.")}


def test_the_workflow_module_cannot_reach_configuration() -> None:
    """The agent may import its contracts, and nothing that reads settings."""
    reachable = _imported_by_the_workflow()
    forbidden = sorted(
        m for m in reachable if any(m == p or m.startswith(p + ".") for p in ENV_READING_PACKAGES)
    )
    assert not forbidden, (
        f"src/agent/agent.py imports {forbidden}, which read os.environ. "
        f"Decide it in a @tool activity and pass the answer on SourceIssue instead."
    )


def test_the_agent_module_never_touches_os_environ() -> None:
    tree = ast.parse(pathlib.Path("src/agent/agent.py").read_text())
    hits = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and ast.unparse(node).startswith("os.environ")
    ]
    assert not hits, hits


def test_the_decisions_that_need_configuration_arrive_as_data() -> None:
    """Each was once computed in the workflow, and each read the keyword."""
    from src.models.schemas import SourceIssue

    for field in ("trigger_is_question", "explanation", "request_is_only_link", "unread_links"):
        assert field in SourceIssue.model_fields, field


# --- file I/O is restricted in a workflow too --------------------------------
#
# The tests above catch os.environ. They did NOT catch reading a file, and a
# version lookup that did `metadata.json.read_text()` on every run reached
# production code review before this test existed. Same sandbox, same error
# class, different call.

_FILE_READS = ("read_text", "read_bytes", "open")


def test_the_workflow_module_never_reads_a_file() -> None:
    """A workflow may not read files. The agent's own version is a constant for
    exactly this reason — see AGENT_VERSION in src/agent/delegated.py."""
    offenders: list[str] = []
    for module in ("src/agent/agent.py", "src/agent/delegated.py"):
        tree = ast.parse(pathlib.Path(module).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name in _FILE_READS:
                offenders.append(f"{module}: {ast.unparse(node)[:80]}")
    assert not offenders, (
        "These run inside the Temporal workflow and would raise "
        f"RestrictedWorkflowAccessError in production: {offenders}. "
        "Read it in a @tool activity, or make it a constant."
    )


def test_the_workflow_module_does_not_import_pathlib_for_reading() -> None:
    """A softer guard on the same mistake: Path(...) in the workflow modules is
    almost always about to become a file read."""
    for module in ("src/agent/agent.py", "src/agent/delegated.py"):
        source = pathlib.Path(module).read_text()
        assert "Path(__file__)" not in source, (
            f"{module} locates itself on disk, which only makes sense in order to "
            "read something. A workflow may not."
        )
