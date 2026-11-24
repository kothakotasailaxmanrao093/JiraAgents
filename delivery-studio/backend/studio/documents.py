"""The PDFs the agents made, and the files people upload."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config


def url(path: Path | str) -> str:
    """The page's link to one document."""
    return "/api/file?path=" + quote(str(path), safe="")


def doc(path: str | None) -> dict[str, str] | None:
    """{name, url} for a document a run made, or None."""
    if not path:
        return None
    p = Path(path)
    return {"name": p.name, "url": url(p)}


def allowed(path: Path) -> bool:
    """Only the agents' output and the uploads are ever served or passed on."""
    path = path.resolve()
    return any(base.resolve() in path.parents for base in (config.OUTPUT, config.UPLOADS, config.DOCS_EXPORT))


def for_card(key: str) -> dict[str, list[dict[str, Any]]]:
    """The reviews and designs made for a card so far, newest first."""
    out = {}
    for kind, folder, stem in (
        ("reviews", "requirement-review", f"{key}-review"),
        ("designs", "planning-agent", f"{key}-TDD"),
    ):
        found = sorted(
            (config.OUTPUT / folder).glob(f"{stem}-v*.pdf"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        out[kind] = [
            {"name": p.name, "path": str(p), "url": url(p), "modified": p.stat().st_mtime}
            for p in found
        ]
    return out


def save_upload(filename: str, data: bytes, content_type: str) -> dict[str, Any]:
    """Keep an uploaded file where the local workers read it (``local-uploads/…``)."""
    import local_files

    saved = local_files.save(filename or "upload", data, content_type or "")
    path = local_files.folder() / saved["key"][len(local_files.PREFIX) :]
    return {**saved, "path": str(path)}


def as_upload(path: str) -> str:
    """A document this studio made, handed to an agent as an upload key."""
    p = Path(path)
    return save_upload(p.name, p.read_bytes(), "application/pdf")["key"]
