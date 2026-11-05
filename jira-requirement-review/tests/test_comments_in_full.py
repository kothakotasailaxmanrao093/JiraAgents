"""Comments reach the review in full; only a huge discussion is shortened (2026-10-15)."""

from context.jira_sources import _issue_documents, _render_comments


def test_every_comment_is_kept_whole():
    comments = [{"author": "PO", "body": "x" * 5000}, {"author": "Dev", "body": "y" * 5000}]
    assert _render_comments(comments) == f"- PO: {'x' * 5000}\n- Dev: {'y' * 5000}"


def test_a_huge_discussion_keeps_the_newest_and_says_so():
    comments = [{"author": "A", "body": f"c{i} " + "z" * 100} for i in range(10)]
    text = _render_comments(comments, limit=350)
    assert text.startswith("(7 older comment(s) left out")
    assert "c9 " in text and "c7 " in text and "c6 " not in text


def test_the_comments_document_is_built_from_them():
    bundle = {"key": "BTS-1", "comments": [{"author": "PO", "body": "answer " * 400}]}
    docs = _issue_documents(bundle, "Target BTS-1", is_target=True, role="target")
    assert docs[0].kind == "target_comments" and docs[0].text.endswith("answer")
