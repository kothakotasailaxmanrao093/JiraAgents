"""Shared test setup. No Temporal, no Jira, no SMTP, no network.

``fake_jira`` stands in for a whole Jira Cloud site: it answers the REST
endpoints the tools actually call, records every request, and hands the test a
handle for asserting what was written.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear every LTW/Gmail/Jira override so defaults are what is tested."""
    for name in (
        "LTW_TRIGGER_KEYWORD",
        "LTW_PROCESSED_LABEL",
        "LTW_AWAITING_LABEL",
        "LTW_CONTEXT_MAX_CHARS",
        "LTW_ATTACHMENT_MAX_FILES",
        "LTW_ATTACHMENT_MAX_BYTES",
        "LTW_ATTACHMENT_MAX_CHARS",
        "LTW_ATTACHMENT_READ_IMAGES",
        "LTW_NOTIFY_EMAILS",
        "LTW_NOTIFY_ON",
        "LTW_READ_ONLY",
        "LTW_ALLOWED_PROJECT_KEYS",
        "LTW_DUPLICATE_THRESHOLD",
        "GMAIL_SENDER",
        "GMAIL_APP_PASSWORD",
        "GMAIL_SMTP_HOST",
        "GMAIL_SMTP_PORT",
        "LTW_WEBHOOK_SECRET",
        "LTW_JIRA_WEBHOOK_SECRET",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "JIRA_PROJECT_KEY",
        "JIRA_BOARD_ID",
        "JIRA_EPIC_LINK_FIELD_ID",
        # The AI-model settings too: production sets LTW_REQUIRE_LLM=true, and
        # the suite runs with no gateway, so leaking it from .env failed every
        # test that exercises the fallback. A test that wants it sets it.
        "LTW_REQUIRE_LLM",
        "LTW_LLM_PROVIDER",
        "LTW_LLM_MODEL",
        "LTW_LLM_MAX_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials good enough to get past the configuration guards."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token-value")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ABC")


@pytest.fixture
def gmail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured Gmail sender and two recipients."""
    monkeypatch.setenv("GMAIL_SENDER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("LTW_NOTIFY_EMAILS", "lead@example.com, pm@example.com")


@pytest.fixture(autouse=True)
def _stand_in_model_breakdown(monkeypatch: pytest.MonkeyPatch, request) -> None:
    """A stand-in for the model's breakdown in pipeline tests.

    Production has no fallback any more — no model, nothing created. The
    end-to-end tests of the pipeline (context, duplicates, Jira writes, reply)
    used to reach a breakdown only through that fallback. They now get the same
    deterministic breakdown explicitly, as a test double for the model, so they
    keep testing the pipeline rather than the model. Tests of the breakdown
    step itself call ``decompose`` directly and are unaffected.
    """
    from src.classification import decompose
    from src.tools import tools

    real = tools.build_breakdown

    async def _fake(requirement, context):
        try:
            # A test that stubs the model's answer gets the real path.
            return await real(requirement, context)
        except decompose.DecompositionError as exc:
            # Only when no model answered at all (the tests' stubbed gateway
            # raises); a model answer that failed its checks must still fail.
            cause = exc.__cause__
            if cause is None or isinstance(cause, decompose.DecompositionError | ValidationError):
                raise
            return decompose.heuristic_breakdown(requirement, context), decompose.GENERATOR_LLM

    monkeypatch.setattr(tools, "build_breakdown", _fake)


@pytest.fixture(autouse=True)
def _no_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches the real AI Gateway. Tests that need a model answer
    stub ``_chat`` themselves, which overrides this.

    Without it, tests that ran the fallback path did so by really dialling the
    gateway and failing to connect — and the SDK client loads ``.env`` when it
    is built, re-adding production's LTW_REQUIRE_LLM=true mid-test.
    """
    from src.classification import decompose

    async def _unreachable(prompt: str) -> str:
        raise ConnectionError("no AI gateway in tests")

    monkeypatch.setattr(decompose, "_chat", _unreachable)


@pytest.fixture(autouse=True)
def _no_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test ever opens an SMTP socket, even if it configures a sender."""

    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a test tried to send a real email")

    from src.notifications import email as notifier

    monkeypatch.setattr(notifier, "_send_sync", _blocked)


# ---------------------------------------------------------------------------
# Fake Jira Cloud
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int = 200, body: Any = None) -> None:
        self.status_code = status_code
        self._body = {} if body is None else body
        self.text = str(self._body)

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://example.atlassian.net"),
                response=httpx.Response(self.status_code, text=self.text),
            )


class FakeJira:
    """An in-memory Jira site covering the endpoints the tools call.

    Constructor knobs shape the failure being tested; attributes record what
    the code under test actually did.

    Attributes used by tests:
        store: issues that already exist. Created issues are appended here too,
            so an idempotent re-run finds them by label exactly as Jira would.
        created: issues this run created, in creation order.
        writes: (method, url) for every POST/PUT that changed Jira.
        requests: (method, url) for every request, read or write.
        sprint_added: issue keys moved into a sprint.
        links: (story_key, epic_key) pairs linked after creation.
    """

    def __init__(
        self,
        *,
        issue_types: list[str] | None = None,
        createmeta_status: int = 200,
        project_status: int = 200,
        search_status: int = 200,
        authenticated: bool = True,
        project_description: str = "",
        epics: dict[str, dict[str, str]] | None = None,
        boards: list[dict[str, Any]] | None = None,
        active_sprints: list[dict[str, Any]] | None = None,
        sprint_supported: bool = True,
        sprint_add_status: int = 204,
        fail_on_summary: str = "",
        reject_inline_parent: bool = False,
        can_edit: bool = True,
        refuse_type_change: bool = False,
        refuse_priority: bool = False,
    ) -> None:
        self.issue_types = issue_types or ["Epic", "Story", "Sub-task"]
        self.createmeta_status = createmeta_status
        self.project_status = project_status
        self.search_status = search_status
        self.authenticated = authenticated
        self.project_description = project_description
        self.epics = epics or {}
        self.boards = boards if boards is not None else [{"id": 1, "name": "Main board"}]
        self.active_sprints = (
            active_sprints if active_sprints is not None else [{"id": 55, "name": "Sprint 7"}]
        )
        self.sprint_supported = sprint_supported
        self.sprint_add_status = sprint_add_status
        self.fail_on_summary = fail_on_summary
        # Some company-managed projects reject `parent` inline on create; the
        # tools then create unparented and link afterwards.
        self.reject_inline_parent = reject_inline_parent
        # The root-ticket edits: permission, a refused type change, and a
        # project with no Priority field on its screen.
        self.can_edit = can_edit
        self.refuse_type_change = refuse_type_change
        self.refuse_priority = refuse_priority
        # (issue_key, body) for every PUT that edited an issue's own fields.
        self.edits: list[tuple[str, dict[str, Any]]] = []

        self.store: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str]] = []
        self.sprint_added: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._next_id = 100

    # -- helpers available to tests -------------------------------------

    def summaries_by_type(self, issue_type: str) -> list[str]:
        """Summaries of everything created with this issue type."""
        return [
            issue["fields"]["summary"]
            for issue in self.created
            if issue["fields"].get("issuetype", {}).get("name", "").lower() == issue_type.lower()
        ]

    # -- request routing -------------------------------------------------

    async def __aenter__(self) -> FakeJira:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append(("GET", url))
        return self._get(url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append(("POST", url))
        self.writes.append(("POST", url))
        return self._post(url, kwargs)

    async def put(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append(("PUT", url))
        self.writes.append(("PUT", url))
        return self._put(url, kwargs)

    # -- endpoint implementations ----------------------------------------

    def _get(self, url: str, kwargs: dict) -> _Response:
        if url.endswith("/myself"):
            return _Response(200 if self.authenticated else 401, {"accountId": "acct-1"})

        if "/issue/createmeta/" in url:
            if self.createmeta_status != 200:
                return _Response(self.createmeta_status, {"errorMessages": ["createmeta"]})
            return _Response(200, {"issueTypes": [{"name": n} for n in self.issue_types]})

        if url.startswith("/rest/api/3/project/"):
            if self.project_status != 200:
                return _Response(self.project_status, {"errorMessages": ["Project not found"]})
            key = url.rsplit("/", 1)[-1]
            return _Response(
                200,
                {
                    "key": key,
                    "id": "1",
                    "name": "Demo Project",
                    "description": self.project_description,
                    "style": "next-gen",
                },
            )

        if "/search/jql" in url:
            if self.search_status != 200:
                return _Response(self.search_status, {"errorMessages": ["search failed"]})
            return _Response(200, {"issues": self._search(kwargs.get("params", {}).get("jql", ""))})

        if url == "/rest/api/3/mypermissions":
            return _Response(
                200, {"permissions": {"EDIT_ISSUES": {"havePermission": self.can_edit}}}
            )

        if url == "/rest/api/3/field":
            return _Response(200, [{"id": "customfield_10014", "name": "Epic Link"}])

        if "/board/" in url and url.endswith("/sprint"):
            if not self.sprint_supported:
                return _Response(400, {"errorMessages": ["Board does not support sprints"]})
            return _Response(200, {"values": list(self.active_sprints)})

        if url.startswith("/rest/agile/1.0/board"):
            return _Response(200, {"values": list(self.boards)})

        if url.startswith("/rest/api/3/issue/"):
            key = url.rsplit("/", 1)[-1]
            spec = self.epics.get(key)
            if spec is None:
                return _Response(404, {"errorMessages": [f"Issue does not exist: {key}"]})
            return _Response(
                200,
                {
                    "key": key,
                    "fields": {
                        "summary": spec.get("summary", f"{key} summary"),
                        "description": spec.get("description", ""),
                        "issuetype": {
                            "name": spec.get("issuetype", "Epic"),
                            "subtask": spec.get("subtask", False),
                        },
                        "project": {"key": spec.get("project", key.split("-")[0])},
                        "subtasks": [{"key": k} for k in spec.get("subtasks", [])],
                        "labels": list(spec.get("labels", [])),
                        "priority": {"name": spec.get("priority", "Medium")},
                    },
                },
            )

        return _Response(404, {"errorMessages": [f"no route for {url}"]})

    def _post(self, url: str, kwargs: dict) -> _Response:
        if url == "/rest/api/3/issue":
            fields = (kwargs.get("json") or {}).get("fields", {})
            summary = fields.get("summary", "")
            type_name = (fields.get("issuetype") or {}).get("name", "")
            is_subtask = "sub" in type_name.lower()
            if self.reject_inline_parent and fields.get("parent") and not is_subtask:
                # Sub-tasks genuinely require an inline parent, so only the
                # Story -> Epic link is refused here.
                return _Response(400, {"errorMessages": ["parent is not on the create screen"]})
            if self.fail_on_summary and self.fail_on_summary in summary:
                return _Response(500, {"errorMessages": ["Simulated Jira failure"]})
            project_key = (fields.get("project") or {}).get("key", "ABC")
            self._next_id += 1
            key = f"{project_key}-{self._next_id}"
            issue = {"key": key, "id": str(self._next_id), "fields": dict(fields)}
            issue["labels"] = list(fields.get("labels") or [])
            self.created.append(issue)
            self.store.append(issue)
            return _Response(201, {"key": key, "id": str(self._next_id)})

        if "/sprint/" in url and url.endswith("/issue"):
            if self.sprint_add_status >= 400:
                return _Response(self.sprint_add_status, {"errorMessages": ["Sprint rejected"]})
            self.sprint_added.extend((kwargs.get("json") or {}).get("issues", []))
            return _Response(self.sprint_add_status)

        if url.endswith("/comment"):
            return _Response(201, {"id": "10001"})

        return _Response(404, {"errorMessages": [f"no route for {url}"]})

    def _put(self, url: str, kwargs: dict) -> _Response:
        if url.startswith("/rest/api/3/issue/"):
            issue_key = url.rsplit("/", 1)[-1]
            body = kwargs.get("json") or {}
            fields = body.get("fields") or {}
            if "parent" in fields:
                if self.reject_inline_parent:
                    return _Response(400, {"errorMessages": ["parent is not supported"]})
                self.links.append((issue_key, (fields["parent"] or {}).get("key", "")))
                return _Response(204)
            # Classic Epic Link: a custom field holding the epic key as a string.
            for name, value in fields.items():
                if name.startswith("customfield_") and isinstance(value, str):
                    self.links.append((issue_key, value))
                    return _Response(204)
            if "issuetype" in fields and self.refuse_type_change:
                return _Response(
                    400, {"errors": {"issuetype": "The issue type selected is invalid."}}
                )
            if "priority" in fields and self.refuse_priority:
                return _Response(400, {"errors": {"priority": "Field 'priority' cannot be set."}})
            self.edits.append((issue_key, body))
            spec = self.epics.get(issue_key)
            if spec is not None:
                if "issuetype" in fields:
                    spec["issuetype"] = fields["issuetype"]["name"]
                for name in ("summary", "description"):
                    if name in fields:
                        spec[name] = fields[name]
                if "priority" in fields:
                    spec["priority"] = fields["priority"]["name"]
                for op in (body.get("update") or {}).get("labels") or []:
                    if "add" in op and op["add"] not in spec.setdefault("labels", []):
                        spec["labels"].append(op["add"])
            return _Response(204)
        return _Response(404, {"errorMessages": [f"no route for {url}"]})

    # -- JQL --------------------------------------------------------------

    def _search(self, jql: str) -> list[dict[str, Any]]:
        """Honour the three JQL shapes the tools build."""
        results = list(self.store)

        if "labels = " in jql:
            wanted = jql.split("labels = ", 1)[1].strip().strip('"')
            results = [
                issue
                for issue in results
                if wanted in (issue.get("labels") or [])
                or wanted in ((issue.get("fields") or {}).get("labels") or [])
            ]

        if "parent = " in jql:
            wanted = jql.split("parent = ", 1)[1].split(" AND ")[0].strip().strip('"')
            results = [
                issue
                for issue in results
                if ((issue.get("fields") or {}).get("parent") or {}).get("key") == wanted
            ]

        return results


@pytest.fixture
def fake_jira(monkeypatch: pytest.MonkeyPatch):
    """Install a fake Jira site in place of the real HTTP client."""

    def install(**kwargs: Any) -> FakeJira:
        fake = FakeJira(**kwargs)
        from src.jira import api as jira

        monkeypatch.setattr(jira, "jira_client", lambda *a, **k: fake)
        return fake

    return install


@pytest.fixture(autouse=True)
def _clean_email_ledger() -> None:
    """Each test starts with an empty repeat-suppression ledger.

    The notifier remembers what it sent so a Temporal retry cannot mail the
    same message again. That memory is per process, so without this one test's
    email silences another's.
    """
    from src.notifications import email as notifier

    notifier.forget_recent_sends()
