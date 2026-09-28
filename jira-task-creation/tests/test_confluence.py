"""Reading Confluence pages a ticket links to.

Teams put the real specification in Confluence. Before this the agent saw the
URL characters and nothing else, then built a thin breakdown without ever
saying why — so these tests care as much about what is *reported* when a page
cannot be read as about the happy path.
"""

from __future__ import annotations

import httpx
import pytest

from src.confluence import client as C

SITE = "https://example.atlassian.net"


def ref(url: str):
    return C.parse_reference(url, SITE)


# --- which URLs name a page -------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks", "id:131181"),
        (f"{SITE}/wiki/spaces/FL/pages/131181", "id:131181"),
        (f"{SITE}/wiki/spaces/FL/pages/131181/", "id:131181"),
        (f"{SITE}/wiki/display/FL/Driver+Breaks", "title:fl:driver breaks"),
        (f"{SITE}/wiki/display/FL/Driver%20Breaks", "title:fl:driver breaks"),
        (f"{SITE}/wiki/pages/viewpage.action?pageId=131181", "id:131181"),
    ],
)
def test_the_page_a_url_names_is_recognised(url: str, expected: str) -> None:
    found = ref(url)
    assert found is not None
    assert found.identity == expected


@pytest.mark.parametrize(
    "url",
    [
        f"{SITE}/browse/FL-15",  # Jira, not Confluence
        f"{SITE}/wiki/spaces/FL/overview",  # a space, not a page
        "not a url at all",
        "",
    ],
)
def test_things_that_are_not_a_page_are_ignored(url: str) -> None:
    assert ref(url) is None


# --- the security boundary --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/wiki/spaces/FL/pages/1/x",
        "https://example.atlassian.net.evil.com/wiki/spaces/FL/pages/1/x",
        "http://169.254.169.254/wiki/spaces/FL/pages/1/x",
        "https://EXAMPLE.ATLASSIAN.NET.attacker.io/wiki/spaces/FL/pages/1/x",
    ],
)
def test_a_page_on_another_host_is_never_fetched(url: str) -> None:
    """Anyone who can comment can put a URL here, so only this site is followed.

    Without the check, a comment could make the agent fetch any address its
    network can reach and paste the response into a Jira ticket.
    """
    assert ref(url) is None


def test_the_same_site_is_matched_case_insensitively() -> None:
    assert ref(f"{SITE.upper()}/wiki/spaces/FL/pages/131181/X") is not None


# --- finding them in what a person wrote ------------------------------------


def test_a_url_in_a_sentence_loses_its_trailing_punctuation() -> None:
    text = f"The spec is at {SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks. Thanks!"
    found = C.find_references([text], SITE)
    assert [r.identity for r in found] == ["id:131181"]


def test_one_page_linked_three_ways_is_read_once() -> None:
    texts = [
        f"{SITE}/wiki/spaces/FL/pages/131181/Driver+Breaks",
        f"{SITE}/wiki/spaces/FL/pages/131181",
        f"{SITE}/wiki/pages/viewpage.action?pageId=131181",
    ]
    assert [r.identity for r in C.find_references(texts, SITE)] == ["id:131181"]


def test_several_distinct_pages_keep_their_order() -> None:
    texts = [f"{SITE}/wiki/spaces/FL/pages/2/B and {SITE}/wiki/spaces/FL/pages/1/A"]
    assert [r.page_id for r in C.find_references(texts, SITE)] == ["2", "1"]


# --- storage format -> text -------------------------------------------------


def test_paragraphs_and_lists_keep_their_reading_order() -> None:
    body = "<p>Rules:</p><ul><li>At least 15 minutes</li><li>No more than 3</li></ul>"
    text = C.storage_to_text(body)
    assert "Rules:" in text
    assert "- At least 15 minutes" in text
    assert text.index("15 minutes") < text.index("No more than 3")


def test_macro_configuration_is_not_mistaken_for_content() -> None:
    """``<ac:parameter>`` holds colour codes and UUIDs no reader ever sees."""
    body = (
        '<ac:structured-macro ac:name="panel">'
        '<ac:parameter ac:name="bgColor">#E3FCEF</ac:parameter>'
        "<ac:rich-text-body><p>Drivers may log a break.</p></ac:rich-text-body>"
        "</ac:structured-macro>"
    )
    text = C.storage_to_text(body)
    assert "Drivers may log a break." in text
    assert "E3FCEF" not in text


def test_entities_and_non_breaking_spaces_become_ordinary_text() -> None:
    assert C.storage_to_text("<p>Tom&nbsp;&amp;&nbsp;Jerry</p>") == "Tom & Jerry"


def test_an_empty_body_is_not_an_error() -> None:
    assert C.storage_to_text("") == ""


# --- fetching ---------------------------------------------------------------


def client_returning(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=SITE, transport=httpx.MockTransport(handler))


def page_payload(text: str = "<p>Drivers may log a break.</p>") -> dict:
    return {
        "id": "131181",
        "title": "Driver Breaks",
        "space": {"key": "FL"},
        "body": {"storage": {"value": text}},
    }


@pytest.mark.asyncio
async def test_a_linked_page_is_read_into_usable_text() -> None:
    async with client_returning(lambda r: httpx.Response(200, json=page_payload())) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/spaces/FL/pages/131181/X"), SITE)
    assert page.usable
    assert page.title == "Driver Breaks"
    assert "Drivers may log a break." in page.text
    assert page.url == f"{SITE}/wiki/spaces/FL/pages/131181"


@pytest.mark.asyncio
async def test_a_page_named_only_by_title_is_looked_up_in_its_space() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"results": [page_payload()]})

    async with client_returning(handler) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/display/FL/Driver+Breaks"), SITE)
    assert seen["spaceKey"] == "FL"
    assert seen["title"] == "Driver Breaks"
    assert page.usable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, fragment",
    [
        (403, "not permitted"),
        (404, "was not found"),
        (500, "could not be read"),
    ],
)
async def test_a_page_that_cannot_be_read_says_so_and_does_not_raise(
    status: int, fragment: str
) -> None:
    async with client_returning(lambda r: httpx.Response(status, json={})) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/spaces/FL/pages/131181/X"), SITE)
    assert not page.usable
    assert fragment in page.note


@pytest.mark.asyncio
async def test_a_missing_title_lookup_names_the_page_it_wanted() -> None:
    async with client_returning(lambda r: httpx.Response(200, json={"results": []})) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/display/FL/Nope"), SITE)
    assert not page.usable
    assert "Nope" in page.note and "FL" in page.note


@pytest.mark.asyncio
async def test_a_long_page_is_truncated_and_says_that_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LTW_CONFLUENCE_MAX_CHARS", "500")
    body = "<p>" + ("word " * 5000) + "</p>"
    async with client_returning(lambda r: httpx.Response(200, json=page_payload(body))) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/spaces/FL/pages/131181/X"), SITE)
    # At most the cap, not exactly it: the schema strips whitespace, and the
    # character the cut landed on here is a space.
    assert 490 <= len(page.text) <= 500
    assert "Truncated" in page.note


# --- orchestration ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_page_is_reported_rather_than_passed_over() -> None:
    """The silent version of this is how a linked spec becomes a thin breakdown."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "999999" in str(request.url):
            return httpx.Response(404, json={})
        return httpx.Response(200, json=page_payload())

    async with client_returning(handler) as c:
        pages, notes = await C.fetch_linked_pages(
            c,
            [f"{SITE}/wiki/spaces/FL/pages/131181/A and {SITE}/wiki/spaces/FL/pages/999999/B"],
            SITE,
        )
    assert [p.usable for p in pages] == [True, False]
    assert len(notes) == 1
    assert "999999" in notes[0]


@pytest.mark.asyncio
async def test_more_pages_than_the_cap_are_capped_and_said_out_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LTW_CONFLUENCE_MAX_PAGES", "2")
    text = " ".join(f"{SITE}/wiki/spaces/FL/pages/{i}/P" for i in range(1, 6))
    async with client_returning(lambda r: httpx.Response(200, json=page_payload())) as c:
        pages, notes = await C.fetch_linked_pages(c, [text], SITE)
    assert len(pages) == 2
    assert "only the first 2" in notes[0]


@pytest.mark.asyncio
async def test_nothing_is_fetched_when_the_feature_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LTW_CONFLUENCE_ENABLED", "false")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("Confluence must not be called when disabled")

    async with client_returning(handler) as c:
        pages, notes = await C.fetch_linked_pages(c, [f"{SITE}/wiki/spaces/FL/pages/1/A"], SITE)
    assert pages == [] and notes == []


@pytest.mark.asyncio
async def test_a_ticket_with_no_links_makes_no_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made")

    async with client_returning(handler) as c:
        pages, notes = await C.fetch_linked_pages(c, ["Just a plain requirement."], SITE)
    assert pages == [] and notes == []


# --- Jira's own linked-pages panel ------------------------------------------


@pytest.mark.asyncio
async def test_urls_come_out_of_the_remote_link_panel() -> None:
    payload = [
        {"object": {"url": f"{SITE}/wiki/spaces/FL/pages/131181/A"}},
        {"object": {"url": ""}},
    ]
    async with client_returning(lambda r: httpx.Response(200, json=payload)) as c:
        urls = await C.fetch_remote_link_urls(c, "FL-15")
    assert urls == [f"{SITE}/wiki/spaces/FL/pages/131181/A"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        ["a bare string, not an object"],
        [{"object": "not a dict"}],
        [{}],
        "not even a list",
    ],
)
async def test_an_oddly_shaped_remote_link_response_yields_nothing_and_never_raises(
    payload,
) -> None:
    """This endpoint is outside the validated issue payload, so its shape is not
    guaranteed. An exception here once killed the entire issue read."""
    async with client_returning(lambda r: httpx.Response(200, json=payload)) as c:
        assert await C.fetch_remote_link_urls(c, "FL-15") == []


@pytest.mark.asyncio
async def test_an_unreachable_remote_link_endpoint_is_not_fatal() -> None:
    async with client_returning(lambda r: httpx.Response(500, json={})) as c:
        assert await C.fetch_remote_link_urls(c, "FL-15") == []


# --- how a linked page reaches the requirement ------------------------------
# These three were all found by running realistic end-to-end scenarios, not by
# the unit tests above.


def test_a_url_never_survives_into_the_requirement_text() -> None:
    """A Jira Story was literally titled "Build what this spec says: https://…".

    A link points at a requirement; it is never the requirement. Whatever is
    behind it arrives through the linked-pages section, or the reply says it
    could not be read.
    """
    from src.context.ingest import clean_requirement_text

    url = "https://example.atlassian.net/wiki/spaces/FL/pages/1/Driver+Breaks"
    assert clean_requirement_text(f"@Aetherion Build what this spec says: {url}", "Aetherion") == (
        "Build what this spec says"
    )
    assert clean_requirement_text(f"@Aetherion {url}", "Aetherion") == ""
    # Text with no link is untouched.
    assert (
        clean_requirement_text("@Aetherion Allow drivers to log a break.", "Aetherion")
        == "Allow drivers to log a break."
    )


def test_a_link_only_comment_uses_the_page_as_the_requirement() -> None:
    """``@Aetherion <link>`` says nothing once the URL is stripped.

    The person did state a requirement — it is on the page. Promoting it also
    keeps the placeholder out of Jira: the Epic summary previously began
    "(no description provided) Driver Rest Breaks - ...".
    """
    from src.context.ingest import assemble_requirement
    from src.models.schemas import ConfluencePage, SourceIssue

    source = SourceIssue(
        key="FL-10",
        summary="Agent requests",
        description="Tracks fleet maintenance work.",
        trigger_comment_id="1",
        trigger_comment_author="Laxman",
        trigger_comment_body="@Aetherion https://example.atlassian.net/wiki/spaces/FL/pages/1/X",
        confluence_pages=[
            ConfluencePage(
                page_id="1",
                title="Driver Rest Breaks",
                space_key="FL",
                url="https://example.atlassian.net/wiki/spaces/FL/pages/1/X",
                text="Allow drivers to log a rest break.",
            )
        ],
    )
    text, _ = assemble_requirement(source)
    assert "(no description provided)" not in text
    assert "Driver Rest Breaks" in text, "the page is named as the origin"
    assert "Allow drivers to log a rest break." in text
    # Promoted, not duplicated into a second section as well.
    assert text.count("Allow drivers to log a rest break.") == 1


# --- files attached to a linked page ----------------------------------------
# A page that says "see the attached sheet" contributes nothing without these,
# and the thin breakdown that follows looks like a bad requirement.


def _attachment_list(*names: str) -> dict:
    return {
        "results": [
            {
                "title": name,
                "extensions": {"mediaType": "text/csv"},
                "_links": {"download": f"/download/attachments/131181/{name}"},
            }
            for name in names
        ]
    }


def _routed(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    """Serve by path suffix, so one client answers page, list and download."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, response in routes.items():
            if request.url.path.endswith(suffix):
                return response
        return httpx.Response(404, json={})

    return httpx.AsyncClient(base_url=SITE, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_file_attached_to_a_linked_page_is_read() -> None:
    routes = {
        "/child/attachment": httpx.Response(200, json=_attachment_list("rules.csv")),
        "/rules.csv": httpx.Response(200, content=b"rule,limit\nbreak,30\n"),
        "/content/131181": httpx.Response(200, json=page_payload("<p>See the attached sheet.</p>")),
    }
    async with _routed(routes) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/spaces/FL/pages/131181/X"), SITE)

    assert [a.filename for a in page.attachments] == ["rules.csv"]
    assert "break" in page.attachments[0].text


@pytest.mark.asyncio
async def test_an_unreadable_page_attachment_is_reported_not_dropped() -> None:
    routes = {
        "/child/attachment": httpx.Response(200, json=_attachment_list("spec.csv")),
        "/spec.csv": httpx.Response(500, text="boom"),
        "/content/131181": httpx.Response(200, json=page_payload()),
    }
    async with _routed(routes) as c:
        pages, notes = await C.fetch_linked_pages(
            c, [f"{SITE}/wiki/spaces/FL/pages/131181/X"], SITE
        )

    assert not pages[0].attachments[0].text
    assert any("spec.csv" in n and "Driver Breaks" in n for n in notes)


@pytest.mark.asyncio
async def test_an_unsupported_page_attachment_is_never_downloaded() -> None:
    downloads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/child/attachment"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "diagram.sketch",
                            "extensions": {"mediaType": "application/octet-stream"},
                            "_links": {"download": "/download/attachments/131181/diagram.sketch"},
                        }
                    ]
                },
            )
        if "download" in path:
            downloads.append(path)
            return httpx.Response(200, content=b"")
        return httpx.Response(200, json=page_payload())

    async with httpx.AsyncClient(base_url=SITE, transport=httpx.MockTransport(handler)) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/spaces/FL/pages/131181/X"), SITE)

    assert downloads == []
    assert "Unsupported" in page.attachments[0].note


@pytest.mark.asyncio
async def test_page_attachments_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LTW_CONFLUENCE_READ_ATTACHMENTS", "false")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=page_payload())

    async with httpx.AsyncClient(base_url=SITE, transport=httpx.MockTransport(handler)) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/spaces/FL/pages/131181/X"), SITE)

    assert page.attachments == []
    assert not any("attachment" in p for p in calls)


@pytest.mark.asyncio
async def test_a_failure_listing_attachments_is_not_fatal() -> None:
    routes = {
        "/child/attachment": httpx.Response(503, text="unavailable"),
        "/content/131181": httpx.Response(200, json=page_payload()),
    }
    async with _routed(routes) as c:
        page = await C.fetch_page(c, ref(f"{SITE}/wiki/spaces/FL/pages/131181/X"), SITE)

    assert page.usable
    assert "could not be listed" in page.attachments[0].note
