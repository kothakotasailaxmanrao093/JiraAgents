"""Files picked on the local form are kept on the laptop (scripts/local_files.py).

The local test form uploaded picked files to the hosted platform, which a laptop
run cannot read back — the files were silently left out (2026-10-06).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import local_files  # noqa: E402


@pytest.fixture(autouse=True)
def uploads_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("LOCAL_UPLOADS_DIR", str(tmp_path))
    return tmp_path


def test_a_picked_file_is_kept_and_read_back(uploads_dir: Path) -> None:
    saved = local_files.save(
        "17185 Transcript.vtt", b"WEBVTT\n\n00:01 Tyler: bulk print", "text/vtt"
    )
    key = saved["key"]
    assert key.startswith("local-uploads/") and key.endswith("/17185 Transcript.vtt")
    assert local_files.read(key) == b"WEBVTT\n\n00:01 Tyler: bulk print"
    assert list(uploads_dir.rglob("*.vtt")), "kept on this laptop"


def test_the_name_seen_by_the_agent_is_the_file_name() -> None:
    # The agents name an upload after the key's last part (uploads.file_name).
    key = local_files.save("screen.png", b"x")["key"]
    assert key.rsplit("/", 1)[-1] == "screen.png"


def test_a_name_cannot_escape_the_folder(uploads_dir: Path) -> None:
    key = local_files.save("../../etc/passwd", b"x")["key"]
    assert key.endswith("/passwd") and ".." not in key
    with pytest.raises(ValueError):
        local_files.read("local-uploads/../../outside.txt")


def test_the_form_keeps_files_here_instead_of_uploading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aetherion_sdk import local_form

    monkeypatch.setattr(local_form, "_upload_to_hosted", local_form._upload_to_hosted)
    local_files.install_form()
    assert local_form._upload_to_hosted is local_files.save_upload
    import asyncio

    saved = asyncio.run(local_form._upload_to_hosted("a.txt", b"hi", "text/plain"))
    assert local_files.read(saved["key"]) == b"hi", "awaited, as the form calls it"


def test_a_worker_reads_local_keys_and_passes_others_to_the_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from common_lib.storage.storage_client import RetrievalMode, storage

    asked: list[tuple] = []
    monkeypatch.setattr(storage, "retrieve", lambda b, k, m: asked.append((b, k)) or b"remote")
    monkeypatch.setattr(
        storage, "init_client", lambda: (_ for _ in ()).throw(RuntimeError("no S3"))
    )
    local_files.install_storage()

    storage.init_client()  # no platform storage on a laptop: not an error
    key = local_files.save("spec.docx", b"local bytes")["key"]
    assert storage.retrieve("laptop", key, RetrievalMode.FULL_OBJECT) == b"local bytes"
    assert storage.retrieve("team-1", "uploads/a.pdf", RetrievalMode.FULL_OBJECT) == b"remote"
    assert asked == [("team-1", "uploads/a.pdf")], "only platform keys reach platform storage"


def test_the_published_agents_never_contain_it() -> None:
    root = Path(__file__).resolve().parents[2]
    for agent in (
        "jira-orchestration",
        "jira-task-creation",
        "jira-requirement-review",
    ):
        for path in (root / agent / "src").rglob("*.py"):
            assert "local_files" not in path.read_text(), f"{path} must not use local_files"
