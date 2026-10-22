"""What to search the code for, taken from a bug report's own words. Pure.

A bug report rarely names the file at fault, but it usually names something the
code also contains: an endpoint path, a class or method name, an exception, an
error message in quotes. Those are the strongest search terms, so they go first.
When the report names none of them, the summary's own nouns are the fallback.

Nothing here calls a model: picking search terms by rule keeps the code search
free, repeatable and explainable ("searched for TransferService and /api/transfer").
"""

from __future__ import annotations

import re

# Identifiers people paste from code or logs.
_BACKTICKED = re.compile(r"`([^`\n]{3,80})`")
_QUOTED = re.compile(r"[\"“]([^\"”\n]{8,120})[\"”]")
_URL_PATH = re.compile(r"(?<![\w.])(/(?:api|v\d)[\w/{}\-]*)")
_EXCEPTION = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Exception|Error))\b")
_CAMEL = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_LOWER_CAMEL = re.compile(r"\b([a-z]+(?:[A-Z][a-z0-9]+)+)\b")
_SNAKE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")
_DOTTED = re.compile(r"\b([A-Za-z_][\w]*\.[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)\(?")
_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")

_STOP = frozenset(
    """
    after again also always another being because before being between both
    cannot could does doing done each every from have having into issue just
    more most must never only other over please same should show shows showing
    some such than that their them then there these they this those through
    under until user users very want were what when where which while with
    would your able about above working works broken wrong incorrect error
    errors fails failing failed problem instead getting returns returning
    investigate correctly properly expected actual currently still since
    successful successfully succeeds succeeded post shows show http https
    """.split()
)

MAX_TERMS = 4


def _clean(term: str) -> str:
    return term.strip().strip(".,;:()[]{}'\"").strip()


def search_terms(summary: str, text: str, *, limit: int = MAX_TERMS) -> list[str]:
    """Up to ``limit`` search strings, strongest first, without repeats.

    ``summary`` is the ticket's title; ``text`` is its description and comments.
    """
    body = f"{summary or ''}\n{text or ''}"
    ordered: list[str] = []

    def add(term: str) -> None:
        term = _clean(term)
        if len(term) < 3 or term.lower() in _STOP:
            return
        if term.lower() not in (t.lower() for t in ordered):
            ordered.append(term)

    for pattern in (_BACKTICKED, _URL_PATH, _EXCEPTION, _DOTTED, _CAMEL, _LOWER_CAMEL, _SNAKE):
        for match in pattern.findall(body):
            # A dotted name that is really a sentence end ("transfer.The") or a
            # file name ("report.pdf") is not a code symbol.
            if pattern is _DOTTED and (" " in match or match.split(".")[-1].lower() in _FILE_EXT):
                continue
            add(match)

    for match in _QUOTED.findall(body):
        add(f'"{match.strip()}"')

    if len(ordered) < limit:
        words = [w for w in _WORD.findall(summary or "") if w.lower() not in _STOP]
        if words:
            # Two or three nouns together ("transfer balance") find the file that
            # handles both; one alone matches half the repository.
            add(" ".join(words[:3]))

    return ordered[:limit]


_FILE_EXT = frozenset(
    "pdf docx doc xlsx xls pptx csv png jpg jpeg gif mp4 mov txt md json com net org io".split()
)


_SPLIT_CAMEL = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
MAX_KEYWORDS = 14


def keywords(summary: str, text: str, *, limit: int = MAX_KEYWORDS) -> list[str]:
    """Lowercase words to score files by: code names first, then the report's nouns.

    ``TransferService`` contributes ``transferservice`` and ``transfer`` and
    ``service``, because the file may be named either way. Words are counted
    across summary and text, and the most repeated go first — a bug report
    repeats its subject.
    """
    body = f"{summary or ''}\n{text or ''}"
    ordered: list[str] = []

    def add(word: str) -> None:
        word = word.lower().strip("_-./")
        if len(word) >= 4 and word not in _STOP and word not in ordered:
            ordered.append(word)

    for term in search_terms(summary, text, limit=8):
        term = term.strip('"')
        for piece in re.split(r"[/.{}\s_\-:()]+", term):
            if piece:
                add(piece)
                for part in _SPLIT_CAMEL.findall(piece):
                    add(part)

    counts: dict[str, int] = {}
    for word in _WORD.findall(body):
        w = word.lower()
        if len(w) >= 4 and w not in _STOP:
            counts[w] = counts.get(w, 0) + 1
    # The summary's words carry double weight: it is the report in one line.
    for word in _WORD.findall(summary or ""):
        w = word.lower()
        if w in counts:
            counts[w] += 1
    for word, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
        add(word)
        if len(ordered) >= limit:
            break
    return ordered[:limit]
