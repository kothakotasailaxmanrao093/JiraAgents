"""``find_unread_links`` (shared/confluence_text.py, mirrored here).

Bug 6: ``find_references`` only ever promises Confluence pages, so a link to
anything else — a Google Doc, a different Atlassian site — was silently
dropped with no record anywhere. This is the other half: everything
``find_references`` did NOT resolve, named rather than lost.
"""

from __future__ import annotations

from shared.confluence_text import find_unread_links

BASE = "https://example.atlassian.net"


def test_a_non_confluence_link_is_named() -> None:
    text = "Build what this spec says: https://docs.google.com/document/d/abc123"
    assert find_unread_links([text], BASE) == ["https://docs.google.com/document/d/abc123"]


def test_a_link_on_a_different_atlassian_site_is_named() -> None:
    text = "See https://other-tenant.atlassian.net/wiki/spaces/X/pages/1/Title"
    assert find_unread_links([text], BASE) == [
        "https://other-tenant.atlassian.net/wiki/spaces/X/pages/1/Title"
    ]


def test_a_real_confluence_page_on_this_site_is_not_named() -> None:
    text = f"See {BASE}/wiki/spaces/FL/pages/131181/Driver+Breaks"
    assert find_unread_links([text], BASE) == []


def test_no_links_at_all_is_an_empty_list_not_an_error() -> None:
    assert find_unread_links(["just plain text"], BASE) == []
    assert find_unread_links([], BASE) == []


def test_the_same_link_repeated_is_named_once() -> None:
    url = "https://docs.google.com/document/d/abc123"
    assert find_unread_links([f"{url} and again {url}"], BASE) == [url]


def test_a_mix_of_readable_and_unreadable_links_only_names_the_unreadable_one() -> None:
    text = (
        f"See {BASE}/wiki/spaces/FL/pages/131181/Driver+Breaks and also "
        "https://docs.google.com/document/d/abc123"
    )
    assert find_unread_links([text], BASE) == ["https://docs.google.com/document/d/abc123"]


def test_jira_client_exposes_the_attribute_gather_context_actually_calls() -> None:
    """Live regression: gather_context_tool.py called ``jira.base_url``, which
    does not exist on JiraClient (the real property is ``.base``) — every
    review run failed with AttributeError until this was caught on a live
    test. This pins the real attribute name directly against the real class,
    so a rename of ``base`` breaks this test immediately instead of only
    breaking in production."""
    from jira.client import JiraClient

    assert hasattr(JiraClient, "base")
    assert not hasattr(JiraClient, "base_url")
