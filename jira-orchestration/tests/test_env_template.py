"""The template must document every variable the code actually reads.

`.env` ships inside the deploy artifact, and the template is the only thing
telling a new person what to put in it. A variable that exists in the code but
not the template is one nobody knows to set — and most of them fail quietly
(a missing `ORCH_NOTIFY_EMAILS` means alerts go nowhere, silently).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".env.template"
SETTINGS = ROOT / "src" / "routing" / "settings.py"


def _declared() -> set[str]:
    return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", TEMPLATE.read_text(), re.MULTILINE))


def _read_by_the_code() -> set[str]:
    source = SETTINGS.read_text()
    return set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]+)"', source)) | set(
        re.findall(r'env_\w+\(\s*"([A-Z][A-Z0-9_]+)"', source)
    )


def test_the_template_exists() -> None:
    assert TEMPLATE.exists(), "a new person has nothing to copy"


def test_every_variable_the_code_reads_is_documented() -> None:
    missing = sorted(_read_by_the_code() - _declared())
    assert not missing, f"read by settings.py but absent from .env.template: {missing}"


def test_the_template_carries_no_secrets() -> None:
    """It is committed. A filled-in token here would be a leak."""
    for line in TEMPLATE.read_text().splitlines():
        if line.startswith(("JIRA_API_TOKEN=", "JIRA_EMAIL=")):
            assert line.split("=", 1)[1].strip() == "", f"template carries a value: {line}"
