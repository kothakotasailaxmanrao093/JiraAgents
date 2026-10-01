"""Context source registry + enablement + target-source document shaping."""

from __future__ import annotations

from context.attachment_source import AttachmentsSource, ConfluenceSource
from context.base import FetchContext
from context.jira_sources import (
    LinkedIssuesSource,
    ParentIssueSource,
    SubtasksSource,
    TargetIssueSource,
)
from context.registry import build_sources
from context.transcript_source import TranscriptSource
from context.trigger_comment_source import TriggerCommentSource


def test_build_sources_order():
    srcs = build_sources(FetchContext(issue_key="ABC-1"))
    assert [type(s) for s in srcs] == [
        TriggerCommentSource,
        TargetIssueSource,
        ParentIssueSource,
        SubtasksSource,
        LinkedIssuesSource,
        AttachmentsSource,
        ConfluenceSource,
        TranscriptSource,
    ]


def test_parent_enabled_only_with_parent_key_and_flag():
    assert not ParentIssueSource().is_enabled(
        FetchContext("ABC-1", target_bundle={"parent_key": None})
    )
    assert ParentIssueSource().is_enabled(
        FetchContext("ABC-1", target_bundle={"parent_key": "ABC-0"})
    )
    assert not ParentIssueSource().is_enabled(
        FetchContext("ABC-1", include_parent=False, target_bundle={"parent_key": "ABC-0"})
    )


def test_subtasks_enabled_only_with_keys_and_flag():
    assert not SubtasksSource().is_enabled(
        FetchContext("ABC-1", target_bundle={"subtask_keys": []})
    )
    assert SubtasksSource().is_enabled(
        FetchContext("ABC-1", target_bundle={"subtask_keys": ["ABC-2"]})
    )
    assert not SubtasksSource().is_enabled(
        FetchContext("ABC-1", include_subtasks=False, target_bundle={"subtask_keys": ["ABC-2"]})
    )


def test_transcript_enabled_only_with_file_key():
    assert not TranscriptSource().is_enabled(FetchContext("ABC-1"))
    assert TranscriptSource().is_enabled(
        FetchContext("ABC-1", transcript_file_keys=["meeting.txt"])
    )


async def test_target_source_emits_description_and_comments():
    ctx = FetchContext(
        issue_key="ABC-1",
        target_bundle={
            "key": "ABC-1",
            "description_text": "Do X",
            "comments": [{"author": "Al", "body": "need detail"}],
        },
    )
    docs = await TargetIssueSource().load(ctx)
    labels = {d.source_label for d in docs}
    assert "Target ABC-1 (description)" in labels
    assert "Target ABC-1 (comments)" in labels
    desc_doc = next(d for d in docs if d.kind == "requirement")
    assert desc_doc.text == "Do X"


async def test_target_source_skips_empty_sections():
    ctx = FetchContext(issue_key="ABC-1", target_bundle={"key": "ABC-1", "description_text": ""})
    docs = await TargetIssueSource().load(ctx)
    assert docs == []


def test_linked_issues_enabled_only_with_links_and_flag():
    assert not LinkedIssuesSource().is_enabled(
        FetchContext("ABC-1", target_bundle={"linked_issues": []})
    )
    assert LinkedIssuesSource().is_enabled(
        FetchContext("ABC-1", target_bundle={"linked_issues": [{"key": "ABC-9"}]})
    )
    assert not LinkedIssuesSource().is_enabled(
        FetchContext(
            "ABC-1",
            include_linked_issues=False,
            target_bundle={"linked_issues": [{"key": "ABC-9"}]},
        )
    )


async def test_linked_issues_source_emits_one_document():
    ctx = FetchContext(
        issue_key="ABC-1",
        target_bundle={
            "key": "ABC-1",
            "linked_issues": [
                {"key": "ABC-9", "link_type": "blocks", "direction": "outward", "summary": "Do Y"},
            ],
        },
    )
    docs = await LinkedIssuesSource().load(ctx)
    assert len(docs) == 1
    assert docs[0].source_label == "Target ABC-1 (linked issues)"
    assert docs[0].kind == "linked_issues"
    assert "ABC-9" in docs[0].text
    assert "Do Y" in docs[0].text
