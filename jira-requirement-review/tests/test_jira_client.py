"""JiraClient against a fake aiohttp transport (no network)."""

from __future__ import annotations

import json

from config import MAX_COMMENTS_PER_ISSUE, JiraSettings
from jira.client import JiraClient


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return json.dumps(self._payload)

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.posted: list[tuple[str, dict]] = []
        self.search_results: list[dict] | None = None

    def get(self, url, params=None):
        if url.endswith("/myself"):
            return FakeResp(
                200, {"displayName": "Bot", "emailAddress": "b@e.com", "accountId": "1"}
            )
        for key, payload in self.issues.items():
            if url.endswith(f"/issue/{key}"):
                return FakeResp(200, payload)
        return FakeResp(404, {"errorMessages": ["not found"]})

    def post(self, url, json=None):
        if url.endswith("/search/jql") or url.endswith("/search"):
            return FakeResp(200, {"issues": self.search_results or []})
        self.posted.append((url, json))
        return FakeResp(201, {"id": "10001"})


def _adf(text):
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _issue(key, *, desc=None, comments=None, parent=None, subtasks=None, links=None):
    fields = {"summary": f"Summary {key}"}
    if desc is not None:
        fields["description"] = _adf(desc)
    if comments:
        fields["comment"] = {
            "comments": [
                {"author": {"displayName": a}, "created": "2026-01-01", "body": _adf(b)}
                for a, b in comments
            ]
        }
    if parent:
        fields["parent"] = {"key": parent}
    if subtasks:
        fields["subtasks"] = [{"key": s} for s in subtasks]
    if links:
        # each link: (link_type_name, direction, target_key, target_summary)
        raw_links = []
        for link_type, direction, target_key, target_summary in links:
            target = {"key": target_key, "fields": {"summary": target_summary}}
            side = "outwardIssue" if direction == "outward" else "inwardIssue"
            raw_links.append({"type": {"name": link_type}, side: target})
        fields["issuelinks"] = raw_links
    return {"key": key, "fields": fields}


def _client():
    session = FakeSession()
    client = JiraClient(JiraSettings("https://x.atlassian.net", "e@e.com", "tok123456"), session)
    return client, session


def test_extract_bundle_pure():
    client, _ = _client()
    bundle = client.extract_bundle(
        _issue(
            "ABC-1",
            desc="Do X",
            comments=[("Al", "need detail")],
            parent="ABC-0",
            subtasks=["ABC-2", "ABC-3"],
        )
    )
    assert bundle["description_text"] == "Do X"
    assert bundle["parent_key"] == "ABC-0"
    assert bundle["subtask_keys"] == ["ABC-2", "ABC-3"]
    assert bundle["comments"][0]["body"] == "need detail"


def test_extract_caps_comments():
    client, _ = _client()
    many = [("A", f"c{i}") for i in range(MAX_COMMENTS_PER_ISSUE + 5)]
    bundle = client.extract_bundle(_issue("ABC-9", desc="d", comments=many))
    assert len(bundle["comments"]) == MAX_COMMENTS_PER_ISSUE


async def test_get_issue_bundle_requests_right_fields():
    client, session = _client()
    captured = {}
    orig = session.get

    def spy(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return orig(url, params)

    session.get = spy
    session.issues["ABC-1"] = _issue("ABC-1", desc="Do X")
    bundle = await client.get_issue_bundle("ABC-1")
    assert bundle["description_text"] == "Do X"
    assert "description" in captured["params"]["fields"]
    assert "parent" in captured["params"]["fields"]
    assert "subtasks" in captured["params"]["fields"]
    assert "comment" in captured["params"]["fields"]
    assert "issuelinks" in captured["params"]["fields"]


async def test_get_parent_bundle_none_when_no_key():
    client, _ = _client()
    assert await client.get_parent_bundle(None) is None


async def test_post_comment_posts_adf_body():
    client, session = _client()
    adf = {"type": "doc", "version": 1, "content": []}
    result = await client.post_comment("ABC-1", adf)
    assert result["id"] == "10001"
    url, body = session.posted[0]
    assert url.endswith("/issue/ABC-1/comment")
    assert body == {"body": adf}


def test_browse_url_with_comment():
    client, _ = _client()
    assert client.browse_url("ABC-1", "555").endswith("/browse/ABC-1?focusedCommentId=555")
    assert client.browse_url("ABC-1").endswith("/browse/ABC-1")


def test_extract_reports_full_subtask_total_when_capped():
    from config import MAX_SUBTASKS

    client, _ = _client()
    keys = [f"ABC-{i}" for i in range(MAX_SUBTASKS + 5)]
    bundle = client.extract_bundle(_issue("ABC-1", desc="d", subtasks=keys))
    assert len(bundle["subtask_keys"]) == MAX_SUBTASKS
    assert bundle["subtask_total"] == MAX_SUBTASKS + 5


def test_extract_bundle_parses_linked_issues():
    client, _ = _client()
    bundle = client.extract_bundle(
        _issue(
            "ABC-1",
            desc="d",
            links=[
                ("blocks", "outward", "ABC-9", "Do Y"),
                ("relates to", "inward", "ABC-2", "Do Z"),
            ],
        )
    )
    assert bundle["linked_issues_total"] == 2
    keys = {li["key"] for li in bundle["linked_issues"]}
    assert keys == {"ABC-9", "ABC-2"}
    outward = next(li for li in bundle["linked_issues"] if li["key"] == "ABC-9")
    assert outward["link_type"] == "blocks"
    assert outward["direction"] == "outward"
    assert outward["summary"] == "Do Y"


def test_extract_caps_linked_issues():
    from config import MAX_LINKED_ISSUES

    client, _ = _client()
    links = [("relates to", "outward", f"ABC-{i}", f"s{i}") for i in range(MAX_LINKED_ISSUES + 3)]
    bundle = client.extract_bundle(_issue("ABC-1", desc="d", links=links))
    assert len(bundle["linked_issues"]) == MAX_LINKED_ISSUES
    assert bundle["linked_issues_total"] == MAX_LINKED_ISSUES + 3


def test_extract_bundle_no_links_is_empty():
    client, _ = _client()
    bundle = client.extract_bundle(_issue("ABC-1", desc="d"))
    assert bundle["linked_issues"] == []
    assert bundle["linked_issues_total"] == 0


async def test_search_jql_returns_keys():
    client, session = _client()
    session.search_results = [{"key": "ABC-1"}, {"key": "ABC-2"}]
    keys = await client.search_jql('project = ABC AND status = "To Do"')
    assert keys == ["ABC-1", "ABC-2"]


async def test_search_jql_empty_results():
    client, session = _client()
    session.search_results = []
    assert await client.search_jql("project = ABC") == []
