"""The reply lands under the comment that asked, like Jira's own "Reply".

Jira behaviour these fakes copy, observed on jiraagentdemo (2026-09-24):
  - POST .../comment with "parentId" creates a reply in that comment's thread;
  - a reply to a reply, or to an unknown id, is refused with 400
    ("Parent comment not found, and no child comments exist...");
  - "parentId" appears on GET .../comment/{id}, NOT in the issue's comment field.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent.router_flow import run_router
from tools import tools

ROOT, REPLY = "10734", "10760"  # a top-level comment, and a reply under it


class FakeJira:
    def __init__(self, *, refuse_parent: bool = False) -> None:
        self.refuse_parent = refuse_parent
        self.posts: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/comment/{REPLY}"):
            return httpx.Response(200, json={"id": REPLY, "parentId": int(ROOT)})
        if request.method == "GET" and path.endswith(f"/comment/{ROOT}"):
            return httpx.Response(200, json={"id": ROOT})
        if request.method == "POST" and path.endswith("/comment"):
            body = json.loads(request.content)
            self.posts.append(body)
            parent = body.get("parentId")
            if parent and (self.refuse_parent or parent == REPLY):
                return httpx.Response(400, json={"errors": {"comment": "Parent comment not found"}})
            return httpx.Response(201, json={"id": "10799"})
        # answered-ids property, label: accept quietly
        if "properties" in path and request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(204)


@pytest.fixture
def jira(monkeypatch):
    def use(fake: FakeJira) -> FakeJira:
        monkeypatch.setattr(
            tools,
            "_client",
            lambda: httpx.AsyncClient(
                base_url="https://example.atlassian.net",
                transport=httpx.MockTransport(fake.handler),
            ),
        )
        monkeypatch.setenv("ORCH_READ_ONLY", "false")
        return fake

    return use


async def _post(thread_id: str) -> dict[str, Any]:
    return await tools.post_reply("BGV-1", [["p", "hello"]], "r1", ROOT, "k", "", thread_id)


async def test_the_reply_is_posted_under_the_triggering_comment(jira) -> None:
    fake = jira(FakeJira())
    result = await _post(ROOT)
    assert result["posted"] is True and result["threaded"] is True
    assert [p.get("parentId") for p in fake.posts] == [ROOT]


async def test_if_jira_refuses_the_thread_the_reply_still_posts_as_a_comment(jira) -> None:
    fake = jira(FakeJira(refuse_parent=True))
    result = await _post(ROOT)
    assert result["posted"] is True and result["threaded"] is False
    assert [p.get("parentId") for p in fake.posts] == [ROOT, None]


async def test_no_thread_id_posts_a_plain_comment(jira) -> None:
    fake = jira(FakeJira())
    result = await _post("")
    assert result["threaded"] is False
    assert [p.get("parentId") for p in fake.posts] == [None]


async def test_a_trigger_that_is_itself_a_reply_is_answered_in_its_parents_thread(jira) -> None:
    jira(FakeJira())
    async with tools._client() as client:
        assert await tools._thread_root(client, "BGV-1", REPLY) == ROOT
        assert await tools._thread_root(client, "BGV-1", ROOT) == ROOT


async def test_the_router_hands_the_thread_to_post_reply() -> None:
    calls: dict[str, tuple] = {}

    async def execute(name, *args, **kwargs):
        calls[name] = args
        if name == "ingress_check":
            return {
                "outcome": "proceed",
                "run_id": "r1",
                "issue_key": "BGV-1",
                "comment_id": REPLY,
                "thread_id": ROOT,
                "comment_body": "@Aetherion help",
                "trigger_keyword": "Aetherion",
            }
        return {"posted": True}

    async def dispatch(*args, **kwargs):  # help is answered by the router itself
        raise AssertionError("help must not dispatch")

    await run_router({"issue_key": "BGV-1", "comment_id": REPLY}, execute, dispatch)
    assert calls["post_reply"][-1] == ROOT
