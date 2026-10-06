"""Reads run together, bounded, and one bad source never sinks the run (2026-09-29).

Measured on BGV-69 before the change: two attachment downloads took 1.2 s
back to back and the Confluence page 1.0 s after them; the project check made
three calls in a row (1.1 s). They are independent, so they now run together.
What a person reads in "what could not be read" must be exactly as before.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.context import attachments as attachments_mod
from src.models.schemas import AttachmentText
from src.tools.tools import create_jira_issues, inspect_jira_context
from tests.test_jira_service import REQUIREMENT, small_breakdown


def item(name: str) -> dict:
    return {"filename": name, "mimeType": "text/plain", "content": f"https://x/{name}"}


async def read_one_at_a_time(items: list[dict], download) -> list[AttachmentText]:
    """The reading loop as it was before, kept here as the reference."""
    out: list[AttachmentText] = []
    for entry in items[: attachments_mod.max_files()]:
        try:
            data = await download(entry["content"])
        except httpx.HTTPError as exc:
            out.append(
                AttachmentText(
                    filename=entry["filename"],
                    mime_type=entry["mimeType"],
                    note=f"Could not download this attachment: {exc}",
                )
            )
            continue
        out.append(await attachments_mod.extract(data, entry["filename"], entry["mimeType"]))
    return out


async def test_one_failing_attachment_gives_the_same_report_as_before() -> None:
    items = [item("a.txt"), item("broken.txt"), item("c.txt")]

    async def download(url: str) -> bytes:
        if "broken" in url:
            raise httpx.ConnectError("connection reset")
        await asyncio.sleep(0.02 if "a.txt" in url else 0)  # finishes last, listed first
        return f"text of {url}".encode()

    parallel, notes = await attachments_mod.read_all(items, download)
    serial = await read_one_at_a_time(items, download)

    assert [a.model_dump() for a in parallel] == [a.model_dump() for a in serial]
    assert [a.filename for a in parallel] == ["a.txt", "broken.txt", "c.txt"]
    assert parallel[1].note == "Could not download this attachment: connection reset"
    assert notes == []


async def test_an_unexpected_error_is_one_unreadable_file_not_a_lost_run() -> None:
    async def download(url: str) -> bytes:
        if "odd" in url:
            raise RuntimeError("storage said no")
        return b"fine"

    read, _ = await attachments_mod.read_all([item("odd.txt"), item("ok.txt")], download)
    assert read[0].note == "Could not read this attachment: storage said no"
    assert read[1].text == "fine"


async def test_concurrency_never_exceeds_the_setting(monkeypatch) -> None:
    monkeypatch.setenv("LTW_READ_CONCURRENCY", "3")
    in_flight = peak = 0

    async def download(url: str) -> bytes:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return b"x"

    read, _ = await attachments_mod.read_all([item(f"{i}.txt") for i in range(10)], download)
    assert len(read) == 10
    assert peak == 3, "bounded, and actually parallel"


async def test_the_file_cap_note_is_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("LTW_ATTACHMENT_MAX_FILES", "2")

    async def download(url: str) -> bytes:
        return b"x"

    read, notes = await attachments_mod.read_all([item(f"{i}.txt") for i in range(5)], download)
    assert len(read) == 2
    assert notes == ["5 attachments found; only the first 2 were read."]


@pytest.mark.parametrize("value,expected", [("", 4), ("0", 1), ("8", 8)])
def test_read_concurrency_defaults_to_four_and_is_at_least_one(monkeypatch, value, expected):
    monkeypatch.setenv("LTW_READ_CONCURRENCY", value)
    assert attachments_mod.read_concurrency() == expected


# --- the project check --------------------------------------------------------------


async def test_the_project_check_still_reports_the_first_problem_first(fake_jira, jira_env):
    fake_jira(project_status=404, search_status=500)
    context = await inspect_jira_context("ABC")
    assert context["ok"] is False
    assert "was not found or is not visible" in context["error"]


async def test_the_project_check_reads_everything_it_needs(fake_jira, jira_env):
    fake = fake_jira()
    context = await inspect_jira_context("ABC", "", "current_sprint")
    assert context["ok"] is True
    assert context["sprint"]["id"] == 55
    assert any("/issue/createmeta/" in url for _, url in fake.requests)


# --- issue types are read once per run ----------------------------------------------


async def test_creation_does_not_ask_for_issue_types_it_was_given(fake_jira, jira_env):
    fake = fake_jira()
    result = await create_jira_issues(
        breakdown=small_breakdown(),
        requirement=REQUIREMENT,
        available_types=["Epic", "Story", "Sub-task"],
    )
    assert result["status"] == "JIRA_CREATED"
    assert not any("/issue/createmeta/" in url for _, url in fake.requests)
