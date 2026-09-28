"""The router runs inside a Temporal workflow, which may not read the world.

`RestrictedWorkflowAccessError` has taken this system's sibling down in
production, and the stack trace was three frames deep into a helper nobody
thought of as configuration. These tests fail the moment the workflow module can
reach one.

The router is the most exposed of the three agents, because almost everything it
does — the keyword, the thresholds, the labels, the model — sounds like
configuration.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import sys

# These read the environment or the network. They run in activities, never in
# the workflow, and the workflow module must not be able to reach them.
ACTIVITY_ONLY = ("routing.settings", "tools")

WORKFLOW_MODULES = ("src/agent/router_flow.py", "src/agent/router_agent.py")


def _imported_by_the_workflow() -> set[str]:
    for name in [m for m in sys.modules if m.split(".")[0] in ("agent", "routing", "tools")]:
        del sys.modules[name]
    before = set(sys.modules)
    importlib.import_module("agent.router_flow")
    return set(sys.modules) - before


def test_the_workflow_module_cannot_reach_configuration() -> None:
    """The sequence may import its contracts, and nothing that reads settings."""
    reachable = _imported_by_the_workflow()
    forbidden = sorted(
        m for m in reachable if any(m == p or m.startswith(p + ".") for p in ACTIVITY_ONLY)
    )
    assert not forbidden, (
        f"agent/router_flow.py imports {forbidden}, which read the environment. "
        "Decide it in a @tool and pass the answer in on the ingress result."
    )


def test_no_module_reachable_from_the_workflow_touches_os_environ() -> None:
    """Catches what the module-allowlist above cannot: a NEW module the
    workflow imports that reads the environment, without anyone remembering to
    add it to ACTIVITY_ONLY first.

    This exists because it would have caught a real bug: task_queue overrides
    were briefly read with os.environ directly inside routing/catalog.py,
    which the workflow imports and which is evaluated at import time — the
    module-allowlist test above did not catch it, because catalog.py was not
    yet a name anyone had thought to forbid.
    """
    reachable = _imported_by_the_workflow()
    offenders: list[str] = []
    for name in sorted(reachable):
        module = sys.modules.get(name)
        source = getattr(module, "__file__", None)
        if not source or not source.endswith(".py"):
            continue
        tree = ast.parse(pathlib.Path(source).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and ast.unparse(node).startswith("os.environ"):
                offenders.append(f"{name}: {ast.unparse(node)}")
    assert not offenders, (
        f"Modules reachable from the workflow read os.environ: {offenders}. "
        "Read it in a @tool and pass the answer in as data instead."
    )


def test_the_workflow_modules_never_touch_os_environ() -> None:
    for module in WORKFLOW_MODULES:
        tree = ast.parse(pathlib.Path(module).read_text())
        hits = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and ast.unparse(node).startswith("os.environ")
        ]
        assert not hits, f"{module}: {hits}"


def test_the_workflow_modules_never_read_a_file() -> None:
    """The version is a constant for exactly this reason."""
    reads = ("read_text", "read_bytes", "open")
    for module in WORKFLOW_MODULES:
        tree = ast.parse(pathlib.Path(module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                assert name not in reads, f"{module} reads a file: {ast.unparse(node)[:80]}"


def test_the_workflow_modules_never_mint_randomness_or_read_the_clock() -> None:
    """Both are non-deterministic, and both would break replay. The run_id is
    minted in the ingress activity for this reason."""
    banned = {"uuid4", "uuid1", "random", "now", "utcnow", "today", "time"}
    for module in WORKFLOW_MODULES:
        tree = ast.parse(pathlib.Path(module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                assert name not in banned, (
                    f"{module} calls {name}(), which is non-deterministic: "
                    f"{ast.unparse(node)[:80]}. Do it in a @tool."
                )
