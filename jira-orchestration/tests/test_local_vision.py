"""Image reading on a laptop run (scripts/local_vision.py, 2026-10-06).

The platform reads every image — attachments, uploads, Confluence images,
pictures inside Word/PDF/PowerPoint files — through one component that needs
the AI Gateway. The local launcher swaps that one method for OpenAI vision.
These run the platform's own readers against a fake OpenAI: no network.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import local_vision  # noqa: E402

ANSWER = (
    "CAPTION: A report list with checkboxes.\n"
    "TEXT: Selected: Archive, Print, Assessment\n"
    "Status Score Name Package Name\n"
    "OBJECTS: checkboxes, search bar"
)


def image(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def openai(monkeypatch: pytest.MonkeyPatch):
    """A fake OpenAI; the platform's vision method restored afterwards."""
    from common_lib.embeddings import vision_processor

    monkeypatch.setattr(
        vision_processor.VisionProcessor,
        "describe_image",
        vision_processor.VisionProcessor.describe_image,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    local_vision._cache.clear()
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ANSWER}}]})

    return sent, httpx.MockTransport(handler)


def test_without_a_key_nothing_is_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert local_vision.install() is False


def test_an_attached_image_is_read_through_the_platform_reader(openai) -> None:
    from agent_lib.utils import file_utils

    sent, transport = openai
    assert local_vision.install(transport=transport)

    text = file_utils.extract_text_from_image(image("PNG"))

    assert "Selected: Archive, Print, Assessment" in text
    content = sent[0]["messages"][0]["content"]
    assert "verbatim OCR" in content[0]["text"], "the platform's own instructions are used"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_picture_inside_a_word_file_is_read(openai) -> None:
    import docx
    from agent_lib.utils import file_utils

    _, transport = openai
    local_vision.install(transport=transport)
    document = docx.Document()
    document.add_paragraph("Bulk print spec: see the Classic screen below.")
    document.add_picture(io.BytesIO(image("PNG")))
    buf = io.BytesIO()
    document.save(buf)

    text = file_utils.extract_text_from_docx(buf.getvalue())

    assert "Bulk print spec" in text and "Selected: Archive, Print, Assessment" in text


def test_formats_openai_does_not_take_are_sent_as_png(openai) -> None:
    sent, transport = openai
    local_vision.describe(image("BMP"), "image/bmp", transport=transport)
    url = sent[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_the_real_type_is_read_from_the_bytes(openai) -> None:
    # The platform passes "image/png" by default, whatever the file is.
    sent, transport = openai
    local_vision.describe(image("JPEG"), "image/png", transport=transport)
    assert sent[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


def test_the_same_image_is_read_once(openai) -> None:
    sent, transport = openai
    data = image("PNG")
    for _ in range(3):
        local_vision.describe(data, transport=transport)
    assert len(sent) == 1


def test_an_unreadable_image_does_not_fail_the_run(openai) -> None:
    from common_lib.embeddings import vision_processor

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    local_vision.install(transport=httpx.MockTransport(broken))
    described = vision_processor.VisionProcessor().describe_image(image("PNG"))
    assert described.is_empty()


def test_the_answer_is_split_into_the_platform_fields() -> None:
    fields = local_vision.split_sections(ANSWER)
    assert fields["caption"] == "A report list with checkboxes."
    assert fields["ocr_text"].startswith("Selected: Archive, Print, Assessment")
    assert "Status Score Name" in fields["ocr_text"]
    assert fields["objects"] == "checkboxes, search bar"


def test_the_published_agents_never_contain_it() -> None:
    root = Path(__file__).resolve().parents[2]
    for agent in (
        "jira-orchestration",
        "jira-task-creation",
        "jira-requirement-review",
    ):
        for path in (root / agent / "src").rglob("*.py"):
            assert "local_vision" not in path.read_text(), f"{path} must not use local_vision"
