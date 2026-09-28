"""Capture real result contracts from each child agent, for the router to be
tested against.

**Why a subprocess per child, rather than importing them.** Each agent is a
separate deployment with its own virtualenv, its own SDK version and its own
HTTP library, and two of them define a top-level `agent` package. Importing both
into one process shadows one with the other — verified, not assumed. Running
each in its own venv is both the only thing that works and an exact mirror of
production, where the contract crosses between processes as JSON.

**Why captured fixtures rather than hand-written ones.** A hand-written contract
proves the router can parse what *I* think a child returns. These are what the
children actually produce, from their real translation code. If a child's output
drifts, `--check` fails and the router's tests are updated with it — rather than
passing happily against a shape nothing produces any more.

    python integration/capture_contracts.py           # write the fixtures
    python integration/capture_contracts.py --check   # fail if they are stale
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "contracts"

# One script per child, run inside that child's own virtualenv. Each prints a
# JSON object of {fixture_name: contract_dict} to stdout.
CHILDREN = {
    "jira-task-creation": r'''
import json, sys
sys.path.insert(0, ".")
from src.agent.delegated import to_contract
from src.models.schemas import ResultStatus

def run(**over):
    base = {
        "status": ResultStatus.JIRA_CREATED.value,
        "summary_text": "Created 1 Story and 3 Sub-tasks.",
        "generator": "model",
        "jira_result": {"created": [
            {"key": "FL-121", "issue_type": "Story", "summary": "Record a tyre pressure check",
             "url": "https://example.atlassian.net/browse/FL-121"},
            {"key": "FL-122", "issue_type": "Sub-task", "summary": "Define the rules"},
            {"key": "FL-123", "issue_type": "Sub-task", "summary": "Implement the behaviour"},
        ]},
        "source_issue": {
            "key": "FL-120",
            "description": "Let depot staff record a tyre pressure check.",
            "comments": [{"body": "a"}, {"body": "b"}, {"body": "c"}],
            "attachments": [
                {"filename": "spec_v1.pdf", "text": "the spec"},
                {"filename": "matrix.xlsx", "text": "", "note": "password-protected"},
            ],
            "confluence_pages": [
                {"title": "Depot Ops Runbook", "text": "", "note": "this account has no access"},
            ],
            "linked_issues": [{"key": "FL-99"}],
        },
        "jira_context": {"issue_count": 111},
        "notification": {"attempted": False, "suppressed": True, "kind": "created"},
        "duplicate_matches": [],
        "clarifying_questions": [],
        "validation_errors": [],
    }
    base.update(over)
    return to_contract(base, run_id="4a81f7c2", agent_version="4.3.0").to_dict()

print(json.dumps({
    "build_created": run(),
    "build_degraded": run(generator="heuristic"),
    "build_needs_info": run(
        status=ResultStatus.CLARIFICATION_REQUIRED.value,
        jira_result={},
        summary_text="Two questions before this can be broken down.",
        clarifying_questions=[
            "Which depots does this apply to?",
            "What happens when the reading is out of range?",
        ],
        notification={"attempted": False, "suppressed": True, "kind": "clarification_required"},
    ),
    "build_not_a_requirement": run(
        status=ResultStatus.OUT_OF_SCOPE.value,
        jira_result={},
        summary_text="This is not a work requirement.",
        notification={"attempted": False, "suppressed": True, "kind": "invalid_request"},
    ),
    "build_already_exists": run(
        status=ResultStatus.READY_FOR_JIRA.value,
        jira_result={},
        summary_text="This work already exists.",
        duplicate_matches=[{
            "proposed_title": "Record a tyre pressure check",
            "existing_key": "FL-9",
            "existing_summary": "Tyre pressure logging",
            "score": 0.81,
            "matched_on": "title similarity",
        }],
        notification={"attempted": False, "suppressed": True, "kind": "duplicates_found"},
    ),
    "build_failed": run(
        status=ResultStatus.JIRA_CREATION_FAILED.value,
        jira_result={},
        summary_text="Jira rejected the write.",
        validation_errors=["403 Forbidden creating Story in FL"],
        notification={"attempted": False, "suppressed": True, "kind": "failed"},
    ),
}, indent=2))
''',
    "jira-requirement-review": r'''
import asyncio, json, sys
sys.path.insert(0, "src")
from agent.delegated import run_delegated

def review(**over):
    base = {
        "status": "success",
        "issue_key": "FL-130",
        "findings": [
            {"category": "ambiguity",
             "description": '"real-time" is used with no latency target given.'},
            {"category": "open_question",
             "description": "What happens when the reading is out of range?"},
        ],
        "readiness": {"level": "needs_major_clarification", "score": 2},
        "warnings": [
            "spec_matrix.xlsx (attached to FL-130) could not be read: password-protected",
        ],
        "documents": [
            {"source_label": "Target FL-130 (description)", "kind": "requirement",
             "text": "Log tyre pressure in real-time."},
            {"source_label": "Attachment requirements_v3.docx (on FL-130)",
             "kind": "attachment", "text": "the spec"},
        ],
        "target_bundle": {"key": "FL-130", "description_text": "Log tyre pressure in real-time."},
    }
    base.update(over)
    return base

async def main():
    async def runner(payload, execute, workflow_id=None):
        return [review()]
    async def runner_nothing(payload, execute, workflow_id=None):
        return [{
            "status": "error", "issue_key": "FL-130", "error": "no_requirement_text",
            "warnings": [], "documents": [],
            "target_bundle": {"key": "FL-130", "description_text": ""},
            "message": "FL-130 has no description, attachment or linked page that could be read.",
        }]
    async def runner_crash(payload, execute, workflow_id=None):
        raise RuntimeError("AI Gateway unreachable")

    async def execute(*a, **k):
        raise AssertionError("delegated mode must not call activities here")

    payload = {"issue_key": "FL-130", "comment_id": "10501", "run_id": "9c03be57",
               "mode": "delegated"}
    print(json.dumps({
        "review_reviewed": await run_delegated(payload, execute, run_review=runner),
        "review_needs_info": await run_delegated(payload, execute, run_review=runner_nothing),
        "review_failed": await run_delegated(payload, execute, run_review=runner_crash),
    }, indent=2))

asyncio.run(main())
''',
}


def capture() -> dict[str, dict]:
    """Run every child in its own venv and collect what it really returns."""
    contracts: dict[str, dict] = {}
    for project, script in CHILDREN.items():
        python = ROOT / project / ".venv" / "bin" / "python"
        if not python.exists():
            raise SystemExit(f"{project} has no virtualenv at {python}")
        result = subprocess.run(
            [str(python), "-c", script], cwd=ROOT / project, capture_output=True, text=True
        )
        if result.returncode != 0:
            print(result.stdout[-2000:], file=sys.stderr)
            print(result.stderr[-4000:], file=sys.stderr)
            raise SystemExit(f"{project} failed to produce contracts")
        contracts.update(json.loads(result.stdout))
    return contracts


def main() -> int:
    check_only = "--check" in sys.argv
    captured = capture()
    OUT.mkdir(parents=True, exist_ok=True)

    stale: list[str] = []
    for name, contract in sorted(captured.items()):
        path = OUT / f"{name}.json"
        want = json.dumps(contract, indent=2, sort_keys=True) + "\n"
        if not path.exists() or path.read_text() != want:
            stale.append(name)
            if not check_only:
                path.write_text(want)

    if check_only:
        if stale:
            print("Captured contracts are STALE:", file=sys.stderr)
            for name in stale:
                print(f"  {name}", file=sys.stderr)
            print(
                "\nA child's output has changed. Re-capture and check the router still "
                "renders it correctly:\n  python integration/capture_contracts.py",
                file=sys.stderr,
            )
            return 1
        print(f"All {len(captured)} captured contracts are current.")
        return 0

    print(f"Captured {len(captured)} real contracts into {OUT}:")
    for name in sorted(captured):
        print(f"  {name}  ({captured[name]['produced_by']} → {captured[name]['outcome']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
