"""Requirement triage and decomposition for the work-breakdown agent.

Two paths, one contract. The LLM path (Aetherion AI Gateway) does the reading;
the heuristic path keeps the agent usable when no gateway is reachable. Both
return objects validated against ``src.models.schemas``, so a malformed model
response is a caught error rather than a bad Jira Epic.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from common_lib.utils.logger import setup_logger
from pydantic import ValidationError

from src.classification.details import (
    Detail,
    carried,
    missing_details,
    target_story,
)
from src.classification.details import describe as describe_details
from src.classification.details import phrase as detail_phrase
from src.classification.self_review import (
    SelfFinding,
    deterministic,
    drop_answered_questions,
    review_draft,
)
from src.classification.validate import ProjectContext, has_action_intent, starts_with_verb
from src.config.settings import env_bool
from src.context.ingest import requirement_core
from src.models.schemas import (
    NO_DEPENDENCIES,
    NOT_SPECIFIED,
    Classification,
    Complexity,
    Epic,
    Priority,
    ResultStatus,
    Story,
    Subtask,
    ValidationVerdict,
    WorkBreakdown,
    allow_source_files_from,
    statement_reads_badly,
)
from src.prompts.templates import (
    SYSTEM_PROMPT,
    breakdown_prompt,
    coverage_instructions,
    duplicate_adjudication_prompt,
    triage_prompt,
)

logger = setup_logger(__name__)

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o"

GENERATOR_LLM = "llm"
GENERATOR_HEURISTIC = "heuristic"


class DecompositionError(RuntimeError):
    """Raised when a decomposition cannot be produced at all."""


def _llm_settings() -> tuple[str, str]:
    provider = os.environ.get("LTW_LLM_PROVIDER", DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
    model = os.environ.get("LTW_LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return provider, model


DEFAULT_MAX_TOKENS = 16000


def _max_tokens() -> int:
    """``LTW_LLM_MAX_TOKENS``, or 16000 — within gpt-4o's 16,384 output cap."""
    raw = os.environ.get("LTW_LLM_MAX_TOKENS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_TOKENS
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


def llm_required() -> bool:
    """When true, a gateway failure fails the run instead of falling back."""
    return env_bool("LTW_REQUIRE_LLM")


def _strip_fences(text: str) -> str:
    """Remove ``` fences a model may wrap its JSON in."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text``; raise on anything else."""
    candidate = _strip_fences(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1:
            raise DecompositionError("Model response contained no JSON object.") from None
        if end <= start:
            raise DecompositionError(
                f"Model response was cut off before its JSON finished "
                f"({len(text)} characters received)."
            ) from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            # The usual cause is an answer cut off at the output-token limit —
            # a large breakdown is long. Named so the reply says what happened
            # instead of "the model was unreachable".
            raise DecompositionError(
                f"Model response was not valid JSON — it may have been cut off "
                f"({len(text)} characters received): {exc}"
            ) from None
    if not isinstance(parsed, dict):
        raise DecompositionError("Model response was not a JSON object.")
    return parsed


async def _chat(prompt: str) -> str:
    """One gateway round-trip. Imported lazily so tests need no gateway."""
    from agent_lib.gateway.ai import AiGatewayClient

    provider, model = _llm_settings()
    async with AiGatewayClient() as client:
        reply = await client.chat(
            provider=provider,
            model_name=model,
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            temperature=0.2,
            # Explicit, and large: with the gateway's default a Large breakdown
            # (an Epic, a dozen Stories, their Sub-tasks) was cut off mid-JSON,
            # failed to parse, and silently became a heuristic breakdown.
            max_tokens=_max_tokens(),
        )
    content = (reply or {}).get("content") or ""
    if not content.strip():
        raise DecompositionError("Model returned an empty response.")
    return content


# --------------------------------------------------------------------------
# Explaining a ticket
# --------------------------------------------------------------------------

_EXPLAIN_PROMPT = """\
Someone asked this question about a Jira ticket:
{question}

Answer it in plain English for a busy colleague, in 3 to 6 sentences: what the
ticket is for, who it serves, and the rules or limits it sets. Use ONLY the
ticket material below — never invent a detail it does not state; if something
the question asks about is not stated, say that plainly. Do not repeat the text
word for word, and do not propose tickets or tasks.

Return JSON only: {{"answer": "<your answer>"}}

TICKET MATERIAL
{material}
"""


async def explain_issue(question: str, material: str) -> str:
    """A plain-English answer to a question about a ticket, written by the model.

    The fallback, ``describe_issue``, can only quote the ticket back — which is
    what BGV-11's "@Aetherion explain this" got (2026-09-24). Raises on any
    model failure so the caller can fall back to it.
    """
    raw = await _chat(_EXPLAIN_PROMPT.format(question=question.strip(), material=material))
    answer = str(_parse_json_object(raw).get("answer") or "").strip()
    if not answer:
        raise DecompositionError("The model returned no explanation.")
    return answer


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------

_VERDICT_TO_STATUS = {
    "VALID": ResultStatus.READY_FOR_JIRA,
    "INVALID": ResultStatus.VALIDATION_ERROR,
    "OUT_OF_SCOPE": ResultStatus.OUT_OF_SCOPE,
    "CLARIFICATION_REQUIRED": ResultStatus.CLARIFICATION_REQUIRED,
}


async def llm_triage(requirement: str, context: ProjectContext) -> ValidationVerdict:
    """Second-opinion triage: scope, relevance, and missing information."""
    raw = await _chat(triage_prompt(requirement, context.as_prompt_text()))
    data = _parse_json_object(raw)

    verdict = str(data.get("verdict", "")).strip().upper()
    status = _VERDICT_TO_STATUS.get(verdict)
    if status is None:
        raise DecompositionError(f"Unrecognised triage verdict: {verdict!r}")

    reason = str(data.get("reason", "")).strip()
    questions = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()][:3]

    if status is ResultStatus.CLARIFICATION_REQUIRED and not questions:
        questions = ["Which details are still needed to describe this requirement?"]

    return ValidationVerdict(
        status=status,
        reason=reason,
        clarifying_questions=questions if status is ResultStatus.CLARIFICATION_REQUIRED else [],
        validation_errors=[reason] if status is ResultStatus.VALIDATION_ERROR and reason else [],
    )


# --------------------------------------------------------------------------
# Near-miss adjudication
# --------------------------------------------------------------------------


async def llm_same_work(pairs: list[tuple[str, str, str]]) -> dict[int, bool]:
    """Ask the model which near-miss pairs are the same work.

    ``pairs`` is ``(proposed_title, existing_key, existing_summary)``. Returns
    ``{index: True}`` only for pairs the model confirms. Anything it omits,
    contradicts or garbles is simply absent, which the caller reads as "not a
    duplicate" — the behaviour before this existed.
    """
    if not pairs:
        return {}
    raw = await _chat(duplicate_adjudication_prompt(pairs))
    data = _parse_json_object(raw)
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        raise DecompositionError("Adjudication response carried no 'verdicts' list.")

    confirmed: dict[int, bool] = {}
    for row in verdicts:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(pairs):
            continue
        if row.get("same") is True:
            confirmed[index] = True
            logger.info(
                f"adjudicated SAME: {pairs[index][0]!r} == {pairs[index][1]} "
                f"({str(row.get('reason', ''))[:80]})"
            )
    return confirmed


# --------------------------------------------------------------------------
# Breakdown
# --------------------------------------------------------------------------


async def llm_breakdown(requirement: str, context: ProjectContext) -> WorkBreakdown:
    """Ask the model for a decomposition and check it before anything is written.

    Checks, with ONE regeneration shared between them:

    * the schema — one rule broken in one story used to fail the whole build
      (BGV-9, 2026-09-24);
    * coverage — every listed line of the requirement must be delivered by a
      Story or Sub-task (BGV-20's red-flag line reached only the Epic);
    * depth and criteria quality (`_quality_problems`);
    * the self-review gate against the shared rubric (`self_review`, D1) —
      stated details dropped, questions the input already answers, boilerplate.

    Schema and coverage failures that survive the regeneration stop the run.
    Quality and rubric findings never do: the better of the two drafts ships,
    with what remains named in the reply.
    """
    lines = requirement_lines(requirement)
    prompt = breakdown_prompt(requirement, context.as_prompt_text(for_breakdown=True))
    if lines:
        prompt = coverage_instructions(lines) + prompt

    first, problem = await _attempt(prompt, lines, want_depth=True)
    findings: list[SelfFinding] = []
    if first is not None:
        findings = await review_draft(requirement, first, _chat)
        rubric = " ".join(f.as_instruction() for f in findings if f.avoidable)
        problem = " ".join(p for p in (problem, rubric) if p)
        if not problem:
            return await _finish(first, requirement, findings, lines)

    logger.warning(f"Breakdown regenerated once: {problem}")
    retry = (
        f"{prompt}\n\nYour previous answer was rejected: {problem} "
        "Return the complete corrected breakdown JSON, fixing exactly that and "
        "keeping everything else. Fill a missing detail only from the requirement "
        "above; if it is not there, ask it as an open question instead."
    )
    second, problem2 = await _attempt(retry, lines, want_depth=False)
    if second is not None:
        # Re-scored in code only: a second model review would be a third call.
        # Questions the first review showed were answered are still dropped.
        answered = [f for f in findings if f.code == "ASKED_WHAT_WAS_STATED"]
        return await _finish(second, requirement, deterministic(second) + answered, lines)
    if first is not None:
        # The regeneration broke what the first draft had right — ship the first.
        return await _finish(first, requirement, findings, lines)
    logger.error(f"Breakdown rejected twice: {problem2}")
    raise DecompositionError(problem2)


async def _finish(
    breakdown: WorkBreakdown,
    requirement: str,
    findings: list[SelfFinding],
    lines: list[str] | None = None,
) -> WorkBreakdown:
    """Drop answered questions, sharpen vague criteria, record what remains."""
    coverage = getattr(breakdown, "_coverage", {}) or {}
    breakdown = drop_answered_questions(breakdown, findings)
    breakdown = await _sharpen_criteria(breakdown, requirement)
    breakdown = await _carry_missing_details(breakdown, requirement, lines or [], coverage)
    remaining = [
        f.as_instruction()
        for f in deterministic(breakdown) + findings
        if f.avoidable and f.code != "ASKED_WHAT_WAS_STATED"
    ]
    # Cannot happen after _carry_missing_details, which copies the line itself
    # as a last resort — named anyway if it ever does, never silent.
    remaining += [
        f'Line {n} of the requirement states "{detail_phrase(d)}" but {where} does not carry it.'
        for n, d, where in missing_details(lines or [], coverage, breakdown)
    ]
    breakdown._quality_notes = list(dict.fromkeys(remaining))
    breakdown._coverage = coverage
    return breakdown


_CARRY_PROMPT = """\
Some stated facts of this requirement are missing from the Stories below. For
each Story, write ONE acceptance criterion per listed fact, as "Given …, when
…, then …", that carries the fact EXACTLY as the requirement words it (same
number, same quoted name, same limit). Use only what the requirement states.

Return JSON only: {{"criteria": {{"<exact Story title>": ["Given …"]}}}}

REQUIREMENT
{requirement}

MISSING
{items}
"""


async def _carry_missing_details(
    breakdown: WorkBreakdown, requirement: str, lines: list[str], coverage: dict[str, Any]
) -> WorkBreakdown:
    """Put every stated fact into the Story it belongs in — guaranteed.

    Asked inside the regeneration, the model left BGV-41's "Price missing",
    "every billing contact", "30 days after the invoice date" and "only one
    reminder" out a second time (2026-09-25). A small request for exactly
    those criteria is followed far more reliably; each is kept only if it
    really carries its fact. Whatever is still missing gets the requirement's
    own sentence as a criterion — faithful by construction, never invented.
    """
    gaps = missing_details(lines, coverage, breakdown)
    if not gaps:
        return breakdown
    wanted: dict[str, list[tuple[int, Detail]]] = {}
    for n, detail, _ in gaps:
        story = target_story(n, lines[n - 1], coverage, breakdown)
        wanted.setdefault(story.title, []).append((n, detail))
    items = "\n".join(
        f'- Story "{title}": '
        + "; ".join(f'"{detail_phrase(d)}" (line {n}: {lines[n - 1]})' for n, d in facts)
        for title, facts in wanted.items()
    )
    proposed: dict[str, list[str]] = {}
    try:
        raw = await _chat(_CARRY_PROMPT.format(requirement=requirement, items=items))
        got = _parse_json_object(raw).get("criteria") or {}
        if isinstance(got, dict):
            proposed = {
                _norm(k): [str(c) for c in v] for k, v in got.items() if isinstance(v, list)
            }
    except Exception as exc:  # noqa: BLE001 — the fallback below still carries every fact
        logger.info(f"Carry request failed, using the requirement's own words: {exc}")

    data = breakdown.model_dump(mode="json")
    added = copied = 0
    for story in data["stories"]:
        facts = wanted.get(story["title"])
        if not facts:
            continue
        offered = proposed.get(_norm(story["title"]), [])
        for n, detail in facts:
            criterion = next(
                (c for c in offered if carried(detail, re.sub(r"\s+", " ", c.lower()))), None
            )
            if criterion:
                added += 1
            else:
                criterion = f'As stated in the requirement: "{lines[n - 1]}"'
                copied += 1
            if criterion not in story["acceptance_criteria"]:
                story["acceptance_criteria"].append(criterion)
    logger.info(f"Carried {added} stated fact(s) by request, {copied} by the requirement's words")
    try:
        carried_breakdown = WorkBreakdown.model_validate(data)
    except ValidationError as exc:
        logger.info(f"Carried criteria broke the schema, keeping the breakdown: {exc}")
        return breakdown
    carried_breakdown._coverage = coverage
    return carried_breakdown


async def _attempt(
    prompt: str, lines: list[str], *, want_depth: bool
) -> tuple[WorkBreakdown | None, str]:
    """One model answer, checked.

    ``(breakdown, "")`` when it passes; ``(breakdown, why)`` when it is usable
    but thin; ``(None, why)`` when it cannot be used at all.
    """
    raw = await _chat(prompt)
    data = _parse_json_object(raw)
    coverage = data.pop("coverage", None)
    try:
        breakdown = _validated_breakdown(data)
    except ValidationError as exc:
        # The full Pydantic dump goes to the log; the reply gets one readable
        # line. Pasted whole, it ran to forty lines of URLs in a Jira comment.
        logger.error(f"Breakdown failed schema validation: {exc}")
        return None, _schema_summary(exc)
    uncovered = uncovered_lines(lines, coverage, breakdown)
    if uncovered:
        listed = "; ".join(f'{i}. "{lines[i - 1]}"' for i in uncovered)
        return None, (
            "The breakdown does not deliver every requirement line — no Story or "
            f"Sub-task covers: {listed}. The Epic does not count."
        )
    # Kept for _finish: the same mapping names what is still missing.
    breakdown._coverage = coverage if isinstance(coverage, dict) else {}
    if want_depth:
        problems = _quality_problems(breakdown)
        gaps = missing_details(lines, coverage, breakdown)
        if gaps:
            problems.append(describe_details(gaps, lines))
        return breakdown, " ".join(problems)
    return breakdown, ""


# A completion criterion that names nothing checkable. BGV-88's was "Emails are
# sent as per the completion criteria" even with the prompt forbidding it.
# Not "works"/"working": "5 working days" is a precise, checkable rule, and
# matching it flagged every criterion that named a working-day limit.
_VAGUE_CRITERION = re.compile(
    r"\b(?:successfully|as expected|as per|as required|is implemented|"
    r"are implemented|functions? correctly|properly|implemented and tested|"
    r"approved and finali[sz]ed|is integrated|and functional)\b",
    re.IGNORECASE,
)


def _vague_subtasks(breakdown: WorkBreakdown) -> list[tuple[Story, Subtask]]:
    return [
        (st, t)
        for st in breakdown.stories
        for t in st.subtasks
        if _VAGUE_CRITERION.search(t.completion_criteria)
        or len(t.completion_criteria.split()) < 6
        or _repeats(t.completion_criteria, t.description)
        or _repeats(t.completion_criteria, t.expected_outcome)
    ]


def _repeats(a: str, b: str) -> bool:
    """Whether a completion criterion only restates another field.

    BGV-32 (2026-09-25) said one thing three times: "Email the generated PDF to
    the client's billing contact" / "…receives the invoice PDF via email" /
    "Email with PDF is sent to the client's billing contact." Most of the
    criterion's meaningful words already being in the other field, with no
    number or example of its own, is a restatement, not a check.
    """
    filler = {"with", "that", "this", "from", "have", "will", "into", "when", "then", "their"}
    words = lambda t: {  # noqa: E731
        w for w in re.findall(r"[a-z]+", t.lower()) if len(w) > 3 and w not in filler
    }
    mine, other = words(a), words(b)
    if not mine or re.search(r"\d", a):
        return False
    return len(mine & other) / len(mine) >= 0.7


_SHARPEN_PROMPT = """\
Rewrite ONLY the completion criteria of the Sub-tasks below. Each must name one
observable check a tester can perform, using the numbers, roles and statuses of
the requirement and the Story's acceptance criteria — e.g. "a document expiring
on 30 June produces a reminder email to its candidate on 31 May". Never use
"works", "implemented", "tested", "successfully", "approved" or "as per". Do not
invent rules the requirement does not state.

Return JSON only: {{"criteria": {{"<exact Sub-task title>": "<new criterion>"}}}}

REQUIREMENT
{requirement}

SUB-TASKS
{items}
"""


async def _sharpen_criteria(breakdown: WorkBreakdown, requirement: str) -> WorkBreakdown:
    """One small request to rewrite the completion criteria nobody can check.

    Asked for inside the breakdown, twice, the model still wrote "Expiry date
    logic is implemented and tested" (BGV-92..102, 2026-09-25). A focused
    request for just those fields is followed far more reliably. Any failure
    keeps the breakdown as it is — it is correct, only less checkable.
    """
    vague = _vague_subtasks(breakdown)
    if not vague:
        return breakdown
    items = "\n".join(
        f"- {t.title} (Story: {st.title}; acceptance criteria: {'; '.join(st.acceptance_criteria)})"
        f"\n  description: {t.description}\n  current: {t.completion_criteria}"
        for st, t in vague
    )
    try:
        raw = await _chat(_SHARPEN_PROMPT.format(requirement=requirement, items=items))
        rewritten = _parse_json_object(raw).get("criteria") or {}
    except Exception as exc:  # noqa: BLE001 — a vaguer criterion beats a failed build
        logger.info(f"Completion criteria not sharpened: {exc}")
        return breakdown
    if not isinstance(rewritten, dict):
        return breakdown
    wanted = {_norm(k): str(v).strip() for k, v in rewritten.items() if str(v).strip()}
    data = breakdown.model_dump(mode="json")
    changed = 0
    for story in data["stories"]:
        for sub in story["subtasks"]:
            new = wanted.get(_norm(sub["title"]))
            if new and not _VAGUE_CRITERION.search(new) and len(new.split()) >= 6:
                sub["completion_criteria"] = new
                changed += 1
    logger.info(f"Sharpened {changed} of {len(vague)} vague completion criteria")
    try:
        return WorkBreakdown.model_validate(data)
    except ValidationError as exc:
        logger.info(f"Sharpened criteria broke the schema, keeping the originals: {exc}")
        return breakdown


def _quality_problems(breakdown: WorkBreakdown) -> list[str]:
    """What the prompt asked for and the answer ignored — asked for once more.

    Each was a prompt rule the model skipped on a live run, so each is checked:
    one Sub-task per Story (BGV-33), no open questions on any Story and
    unverifiable completion criteria (BGV-79..88, 2026-09-25). Only ever a
    reason to retry; a second answer is accepted whatever its depth.
    """
    problems: list[str] = []
    thin = [st.title for st in breakdown.stories if len(st.subtasks) < 2]
    if thin:
        problems.append(
            "These Stories have only one Sub-task: "
            + "; ".join(f'"{t}"' for t in thin)
            + ". Split each into the 2 to 4 distinct pieces of work it needs."
        )
    few_criteria = [st.title for st in breakdown.stories if len(st.acceptance_criteria) < 2]
    if few_criteria:
        problems.append(
            "These Stories have fewer than two acceptance criteria: "
            + "; ".join(f'"{t}"' for t in few_criteria)
            + ". Add the boundary or failure case of the stated rule, as Given/When/Then."
        )
    awkward = [
        st.title for st in breakdown.stories if statement_reads_badly(st.user_story_statement)
    ]
    if awkward:
        problems.append(
            'These user stories put a bare verb right after "I want": '
            + "; ".join(f'"{t}"' for t in awkward)
            + '. Write "I want to <verb>", "I want the <thing>" or "I want <someone> to <verb>".'
        )
    vague = [t.title for _, t in _vague_subtasks(breakdown)]
    if vague:
        problems.append(
            "These Sub-tasks have completion criteria nobody can check: "
            + "; ".join(f'"{t}"' for t in vague[:8])
            + ". Give each an observable check with the stated numbers, e.g. "
            '"a case marked Completed at 10:00 produces an email to its recruiter by 11:00".'
        )
    return problems


def _validated_breakdown(data: dict[str, Any]) -> WorkBreakdown:
    # A Small breakdown must not carry an epic; tolerate a null the model may emit.
    if data.get("epic") in (None, {}, ""):
        data.pop("epic", None)
    return WorkBreakdown.model_validate(data)


_LINE_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")


def requirement_lines(requirement: str) -> list[str]:
    """The listed lines of the STATED requirement — its bullets or numbered items.

    Only the stated requirement: attachments and linked pages are supporting
    material, and holding the model to every line of a whole spec would fail
    good breakdowns. A single sentence has no lines to check.
    """
    text = requirement or ""
    if "## " in text:
        blocks = re.split(r"(?m)^## ", text)
        stated = next((b for b in blocks if b.startswith("Stated requirement")), "")
        text = stated.split("\n", 1)[1] if "\n" in stated else ""
    # Assembly joins the opening sentence and the first bullet onto one line
    # ("…has declared. - Candidate uploads…"); put the bullet back on its own.
    text = re.sub(r"(?<=[.:;!?])[ \t]+(?=[-*•][ \t])", "\n", text)

    # Assembly also glues the NEXT section's heading onto the end of the last
    # bullet ("…no further reminders are sent. Out of scope:"). Under an
    # out-of-scope heading nothing is to be built, so nothing is to be covered:
    # BGV-40 failed outright because "Online card payment" had no Story
    # (2026-09-25).
    items: list[str] = []
    excluded = False
    for line in text.splitlines():
        heading = _TRAILING_HEADING.search(line)
        body = line[: heading.start()].rstrip() if heading else line
        stripped = body.strip()
        match = _LINE_ITEM.match(body)
        if match and not excluded:
            items.append(match.group(1).strip())
        elif not match and stripped:
            # A plain line starts a new section; it is out of scope only if it
            # is itself an out-of-scope heading.
            excluded = bool(_OUT_OF_SCOPE.search(stripped))
        if heading:
            excluded = bool(_OUT_OF_SCOPE.search(heading.group(0)))
    return items if len(items) >= 2 else []


# A section heading at the end of a line: "… sent. Out of scope:".
_TRAILING_HEADING = re.compile(r"(?:(?<=[.!?])\s+|^\s*)([A-Z][^.!?:\n]{1,40}):\s*$")
_OUT_OF_SCOPE = re.compile(
    r"\b(?:out of scope|not in scope|out-of-scope|excluded|exclusions|non-goals|not included)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(text).lower()).strip()


def uncovered_lines(lines: list[str], coverage: Any, breakdown: WorkBreakdown) -> list[int]:
    """Line numbers no Story or Sub-task delivers, by the model's own mapping.

    A mapped title must name a real Story or Sub-task — mapping a line to the
    Epic, or to a title that does not exist, leaves it uncovered.
    """
    if not lines:
        return []
    titles = [_norm(s.title) for s in breakdown.stories]
    titles += [_norm(t.title) for s in breakdown.stories for t in s.subtasks]
    mapping = coverage if isinstance(coverage, dict) else {}
    missing: list[int] = []
    for number in range(1, len(lines) + 1):
        named = mapping.get(str(number), mapping.get(number))
        names = named if isinstance(named, list) else [named]
        hit = any(
            n and any(_norm(n) == t or (len(_norm(n)) > 5 and _norm(n) in t) for t in titles)
            for n in names
        )
        if not hit:
            missing.append(number)
    return missing


def _schema_summary(exc: ValidationError) -> str:
    """ "missing: a, b; unexpected: c; invalid: d" — what a person can act on."""
    missing, unexpected, invalid = [], [], []
    for err in exc.errors():
        field = ".".join(str(p) for p in err.get("loc", ())) or "(top level)"
        kind = err.get("type", "")
        if kind == "missing":
            entry, bucket = field, missing
        elif kind == "extra_forbidden":
            entry, bucket = field, unexpected
        else:
            # A rule the answer broke — keep the rule's own words, they say why.
            msg = str(err.get("msg", "")).removeprefix("Value error, ")[:120]
            entry, bucket = (f"{field} — {msg}" if msg else field), invalid
        if entry not in bucket:
            bucket.append(entry)
    parts = [
        f"{label}: {', '.join(fields[:6])}"
        for label, fields in (
            ("missing", missing),
            ("unexpected", unexpected),
            ("invalid", invalid),
        )
        if fields
    ]
    return "The AI model's answer did not match the breakdown format (" + "; ".join(parts) + ")."


# --------------------------------------------------------------------------
# Heuristic fallback
# --------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"\s*(?:[;\n\r]|,\s*and\s+|,|\band\s+also\b|\band\b|\bplus\b)\s*")
_STOP_PREFIXES = (
    "we need to",
    "we need",
    "i need to",
    "i need",
    "please",
    "the system should",
    "the system must",
    "system should",
    "users should",
    "it should",
    "should",
)

# Clauses opening with one of these are explaining the requirement, not adding a
# capability to it. Splitting prose on commas and "and" otherwise inflates the
# size of a single well-argued requirement.
_EXPLANATORY_PREFIXES = (
    "because",
    "which",
    "since",
    "so that",
    "today",
    "currently",
    "when ",
    "while ",
    "however",
    "although",
    "though",
    "instead",
    "rather",
    "as a result",
    "that is",
    "this is",
    "it is",
    "they are",
    "meaning",
    "for example",
    "e.g",
    "i.e",
)

# A clause longer than this with no action word in it is commentary, not a
# capability; a short one is treated as a list item ("OTP verification").
_MAX_NOUN_PHRASE_WORDS = 6

# "When a job is declined, notify …" — the opening clause is a trigger, and the
# capability is what follows it.
_USER_STORY_RE = re.compile(r"^\s*as an?\s+.{2,40}?,\s*i\s+want\b", re.IGNORECASE)

_CONDITION_RE = re.compile(
    r"^\s*(when|whenever|if|after|before|once|unless|while|as soon as)\b", re.IGNORECASE
)


# --------------------------------------------------------------------------
# Turning a raw clause into a capability phrase
# --------------------------------------------------------------------------

# Requester framing: how someone asks for a thing, not the thing itself.
_REQUESTER_FRAMING = (
    "we would like to",
    "we would like",
    "i would like to",
    "i would like",
    "it would be good if",
    "it would be nice if",
    "the client wants to",
    "the client wants",
    "the customer wants to",
    "the customer wants",
    "we want to",
    "we want",
    "can we please",
    "can we",
    "could we",
    "please could you",
    "please can you",
    "please",
)

# Modal padding that adds no meaning to an imperative phrase.
_MODAL_PADDING = (
    "should be able to",
    "must be able to",
    "needs to be able to",
    "need to be able to",
    "to be able to",
    "should be",
    "must be",
    "has to",
    "have to",
    "needs to",
    "need to",
)

# Verbs that introduce an actor: "allow tenants to X" → actor tenants, action X.
_ACTOR_VERBS = ("allow", "allows", "let", "lets", "enable", "enables", "permit", "permits")

# "let them view ..." names no actor — the pronoun has no antecedent here, so
# it must not be carried into the story as if it were one.
_PRONOUN_ACTORS = frozenset(
    "them they it he she us we you me him her someone anyone everyone".split()
)
_GENERIC_ACTOR = "user of this product"

# Jira caps every summary at 255. One constant, used everywhere.
MAX_SUMMARY_CHARS = 255


def _strip_leading(text: str, phrases: tuple[str, ...]) -> str:
    """Remove any of ``phrases`` from the front of ``text``, repeatedly."""
    changed = True
    while changed:
        changed = False
        lowered = text.lower().lstrip()
        for phrase in phrases:
            if lowered.startswith(phrase):
                text = text.lstrip()[len(phrase) :].lstrip(" ,:-")
                changed = True
                break
    return text.strip()


def _deshout(text: str) -> str:
    """Turn ALL-CAPS prose into sentence case.

    A requirement typed in caps produced a shouting Jira title and a statement
    that read "I want to mARK A DELIVERY COMPLETE" — the first-letter lowercase
    applied to text that was already upper. Acronyms of four letters or fewer
    are left alone so CSV, PDF and GPS survive.
    """
    words = [w for w in text.split() if any(ch.isalpha() for ch in w)]
    if len(words) < 3:
        return text
    upper = [w for w in words if w.isupper()]
    # Judge the phrase, not each word. A per-word length exemption kept short
    # words like "MARK" upper, which then became "mARK" once the statement
    # lowercased the first letter. An occasional acronym in otherwise normal
    # prose ("as CSV and PDF") leaves this ratio well under the threshold.
    if len(upper) / len(words) < 0.6:
        return text
    return text.lower()


def normalise_capability(clause: str) -> str:
    """Reduce a raw clause to a bare capability phrase.

    ``We would like every tenant to be able to update their contact number``
    becomes ``every tenant update their contact number``. Requester framing and
    modal padding say how the request was made, not what is being asked for.
    """
    text = _strip_leading(_deshout(clause.strip(" .;,-–—\t")), _REQUESTER_FRAMING)
    text = _strip_leading(text, _MODAL_PADDING)
    # Padding can also sit mid-phrase: "every tenant to be able to update ..."
    for phrase in _MODAL_PADDING:
        pattern = re.compile(rf"\s+{re.escape(phrase)}\s+", re.IGNORECASE)
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,:-")


# An actor is a short noun phrase. Anything longer, or containing a verb, is
# the start of the action — not the actor.
_MAX_ACTOR_WORDS = 3
_ARTICLES = ("a ", "an ", "the ")


def _plausible_actor(candidate: str) -> bool:
    """True when ``candidate`` reads as a person/role rather than a phrase.

    Guards the ``to``-split below. In "dispatchers assign a job to a driver"
    the first " to " is the indirect object, so a naive split hands back the
    actor "dispatchers assign a job" and the action "a driver". Requiring the
    actor to be short and verb-free rejects that and falls through to the
    first-noun rule instead.
    """
    words = candidate.split()
    if not words or len(words) > _MAX_ACTOR_WORDS:
        return False
    return not has_action_intent(candidate)


def _split_first_noun(rest: str) -> tuple[str, str]:
    """``a driver report a fault`` → ``("driver", "report a fault")``."""
    text = rest.strip()
    lowered = text.lower()
    for article in _ARTICLES:
        if lowered.startswith(article):
            text = text[len(article) :].strip()
            break
    words = text.split()
    if len(words) < 2:
        return _GENERIC_ACTOR, rest
    if words[0].lower() in _PRONOUN_ACTORS:
        return _GENERIC_ACTOR, " ".join(words[1:]).strip()
    return _singular(_clean_actor(words[0])), " ".join(words[1:]).strip()


def extract_actor(clause: str) -> tuple[str, str]:
    """Split ``allow tenants to X`` into ``("tenants", "X")``.

    Returns ``("user of this product", <clause>)`` when no actor is stated, so
    the caller always has something to put in "As a ...".
    """
    text = normalise_capability(clause)
    story = _USER_STORY_RE.match(text)
    if story:
        actor = re.match(r"^\s*as an?\s+(.{2,40}?),", text, re.IGNORECASE).group(1)
        action = re.split(r",\s*i\s+want\s*", text, maxsplit=1, flags=re.IGNORECASE)[1]
        action = re.split(r",\s*so that\b", action, maxsplit=1, flags=re.IGNORECASE)[0]
        return _singular(_clean_actor(actor)), to_infinitive(action.strip())
    lowered = text.lower()
    for verb in _ACTOR_VERBS:
        if lowered.startswith(verb + " "):
            rest = text[len(verb) :].strip()
            # "tenants to download a receipt" → actor "tenants", action "download..."
            match = re.match(r"(.{2,40}?)\s+to\s+(.+)", rest, re.IGNORECASE)
            if match:
                actor = match.group(1).strip(" ,")
                action = match.group(2).strip()
                if actor and action and _plausible_actor(actor):
                    if actor.lower() in _PRONOUN_ACTORS:
                        return _GENERIC_ACTOR, action
                    return _singular(_clean_actor(actor)), action
            # Either no "to", or the text before it was not an actor:
            # "dispatchers assign a job to a driver", "a driver report a fault".
            return _split_first_noun(rest)
    # "every tenant update their contact number"
    match = re.match(r"(every|each|any|all)\s+(\w+)\s+(.+)", text, re.IGNORECASE)
    if match:
        return _singular(_clean_actor(match.group(2))), match.group(3).strip()
    # "operations managers could see the total fuel cost ..." — a modal names
    # the actor just as clearly as "allow <role> to ...", and the modal itself
    # carries no meaning once the action is stated imperatively.
    match = re.match(
        r"(.{2,40}?)\s+(?:could|can|should|must|may|will|would)\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if match:
        actor = match.group(1).strip(" ,")
        action = match.group(2).strip()
        if action and _plausible_actor(actor):
            if actor.lower() in _PRONOUN_ACTORS:
                return _GENERIC_ACTOR, action
            return _singular(_clean_actor(actor)), action
    return _GENERIC_ACTOR, text


def _clean_actor(actor: str) -> str:
    """Trim an article and normalise case: "an Operations Manager" → "operations manager"."""
    text = actor.strip(" ,")
    lowered = text.lower()
    for article in _ARTICLES:
        if lowered.startswith(article):
            text = text[len(article) :].strip()
            break
    # ALL CAPS input should not shout back out of the story statement.
    if text.isupper():
        text = text.lower()
    return text[:1].lower() + text[1:] if text else text


def _singular(actor: str) -> str:
    """ "tenants" → "tenant". Crude but right for the actor nouns that appear."""
    words = actor.split()
    if not words:
        return actor
    last = words[-1]
    if last.lower().endswith("ies") and len(last) > 4:
        words[-1] = last[:-3] + "y"
    elif last.lower().endswith("sses"):
        words[-1] = last[:-2]
    elif last.lower().endswith("s") and not last.lower().endswith(("ss", "us", "is")):
        words[-1] = last[:-1]
    return " ".join(words)


def to_infinitive(action: str) -> str:
    """Make the action read correctly after "I want to ...".

    Strips a leading "to" so it is never doubled, and lowercases the first
    letter so the sentence does not restart mid-clause.

    An acronym is left alone: lowercasing the first letter of "SMS
    notifications" produced Stories and user stories reading "sMS
    notifications".
    """
    text = re.sub(r"^to\s+", "", action.strip(), flags=re.IGNORECASE)
    if not text:
        return text
    first = text.split(maxsplit=1)[0]
    if len(first) > 1 and first[:2].isupper():
        return text
    return text[:1].lower() + text[1:]


def _clean_clause(clause: str) -> str:
    text = clause.strip(" .;,-–—\t")
    lowered = text.lower()
    for prefix in _STOP_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip(" .,:-")
            break
    return text.strip()


# Markdown headings introduced by requirement assembly, e.g. "## Discussion on
# the ticket". Stripped before splitting so they never become capabilities.
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+.*$", re.MULTILINE)


def strip_section_headings(text: str) -> str:
    """Remove markdown section headings and collapse the blank lines they leave.

    A requirement assembled from a Jira issue is sectioned for the model's
    benefit. Those headings describe where the text came from, so anything that
    turns prose into ticket wording — capability splitting, the Epic summary,
    the business objective — must not see them.
    """
    without = _HEADING_RE.sub("", text or "")
    return re.sub(r"\n{2,}", "\n", without).strip()


# Words that open a phrase describing something, rather than asking for it.
_MODIFIER_OPENERS = (
    "with",
    "without",
    "for",
    "from",
    "by",
    "in",
    "on",
    "at",
    "of",
    "to",
    "including",
    "include",
    "using",
    "under",
    "over",
    "about",
    "across",
    "such as",
    "along with",
    "as well as",
)


def _is_modifier(fragment: str, previous: str) -> bool:
    """True when a fragment describes the previous capability rather than adding one.

    "Create me a dashboard with all the drivers and vehicle, with all the
    tracker" is **one** thing. Splitting on "and" and the comma turned it into
    three — a half sentence, an invented "Create vehicle", and "With all the
    tracker", which is not even a sentence. All three reached Jira as Stories.

    A fragment is a modifier when it asks for nothing of its own: no action
    verb, and either it opens with a preposition, or it is a bare noun trailing
    a phrase that already had one.
    """
    text = (fragment or "").strip().lower()
    if not text or has_action_intent(text):
        return False  # it asks for something; it stands alone

    words = text.split()
    # Match the opening WORD, not a prefix of it. `startswith("in")` also
    # matches "insurance", which merged "insurance renewal" into the clause
    # before it and turned six capabilities into five.
    if words[0] in _MODIFIER_OPENERS or " ".join(words[:2]) in _MODIFIER_OPENERS:
        return True  # "with all the tracker"

    # "…with all the drivers and vehicle" — a bare noun continuing a list that
    # belongs to the previous phrase's preposition.
    previous_words = previous.lower().split()
    if len(words) <= 3 and any(word in _MODIFIER_OPENERS for word in previous_words):
        return True
    return False


def _rejoin(previous: str, fragment: str) -> str:
    """Put a modifier back with the word that joined it.

    The split threw away the "and" and the comma. Gluing the pieces back bare
    produced "…with all the drivers vehicle with all the tracker" — structurally
    one capability, but no longer a sentence, and that wording then appeared in
    the Story title and all three of its sub-tasks.

    A fragment that opens with its own preposition reads as a new clause, so it
    takes a comma; a bare noun is a list item, so it takes "and".
    """
    first = fragment.strip().split()[0].lower() if fragment.strip() else ""
    if first in _MODIFIER_OPENERS:
        return f"{previous}, {fragment}"
    return f"{previous} and {fragment}"


def _merge_modifiers(parts: list[str]) -> list[str]:
    """Fold modifier fragments back into the capability they describe."""
    merged: list[str] = []
    for part in parts:
        cleaned = (part or "").strip()
        if not cleaned:
            continue
        if merged and _is_modifier(cleaned, merged[-1]):
            merged[-1] = _rejoin(merged[-1], cleaned)
            continue
        merged.append(cleaned)
    return merged


# Nouns that introduce a list of *values*, not a list of work. "A request moves
# through four states: Draft, Submitted, Approved, and Rejected" describes one
# workflow; splitting on its commas produced four Stories, three of them named
# after a status.
_ENUMERATION_CUE_RE = re.compile(
    r"\b(?:state|status|stage|step|phase|option|value|type|kind|role|level|field"
    r"|column|tier|category|categories|priority|priorities|status(?:es)?)\w*\s*:",
    re.IGNORECASE,
)

# Stand-ins for separators inside such a list, so the splitter cannot see them.
_COMMA_HOLD, _AND_HOLD, _OR_HOLD = "\x00", "\x01", "\x02"


def _hide_enumerations(text: str) -> str:
    """Neutralise the separators inside "…states: A, B and C" style lists."""
    out = text or ""
    for match in list(_ENUMERATION_CUE_RE.finditer(out)):
        start = match.end()
        # The list runs to the end of the sentence or the line, whichever first.
        end = len(out)
        for stop in (".", "!", "?", "\n"):
            found = out.find(stop, start)
            if found != -1:
                end = min(end, found)
        span = out[start:end]
        span = span.replace(",", _COMMA_HOLD)
        span = re.sub(r"\s+and\s+", _AND_HOLD, span, flags=re.IGNORECASE)
        span = re.sub(r"\s+or\s+", _OR_HOLD, span, flags=re.IGNORECASE)
        out = out[:start] + span + out[end:]
    return out


def _restore_enumerations(text: str) -> str:
    return text.replace(_COMMA_HOLD, ",").replace(_AND_HOLD, " and ").replace(_OR_HOLD, " or ")


def split_capabilities(requirement: str) -> list[str]:
    """Split a requirement into distinct capability clauses.

    Only used by the heuristic path and by sizing; it counts what the text
    actually asks for rather than how long the text is.

    A list of values enumerated after a noun ("states: Draft, Submitted, …") is
    held together first: it describes one capability, however many commas it
    contains.
    """
    hidden = _hide_enumerations(requirement)

    # A line of its own is a boundary the writer put there. Splitting the whole
    # blob at once let the continuation rule swallow a complete sentence that
    # happened to start with a noun: a ticket description and a linked
    # Confluence page became one Story joined by "and". Bullet lists keep the
    # single-pass path, which already understands them.
    lines = [line for line in hidden.splitlines() if line.strip()]
    is_bulleted = any(line.strip().startswith(("-", "*", "\u2022")) for line in lines)
    if len(lines) > 1 and not is_bulleted:
        out: list[str] = []
        for line in lines:
            out.extend(_split_capabilities(line))
        return [_restore_enumerations(part) for part in out]

    return [_restore_enumerations(part) for part in _split_capabilities(hidden)]


def _split_capabilities(requirement: str) -> list[str]:
    """The splitter itself, run on text whose value-lists are already hidden."""
    # Markdown section headings describe the *source* of the text (they are added
    # when a requirement is assembled from a Jira issue), not something to build.
    # Left in, each heading becomes a capability and then a Story titled
    # "## Stated requirement (from TT2-85: ...)".
    requirement = strip_section_headings(requirement)

    # Someone pasting a finished story statement means one capability, even
    # though "As a driver, I want …" contains a comma.
    story_form = _USER_STORY_RE.match(requirement.strip())
    if story_form:
        return [_clean_clause(requirement)]

    # Bullet lists are the strongest signal, so honour them before prose splitting.
    bullets = [
        line.strip(" \t-*•")
        for line in requirement.splitlines()
        if line.strip().startswith(("-", "*", "•"))
    ]
    if len(bullets) >= 2:
        # Someone wrote a list on purpose. Each bullet is a capability by
        # definition, so modifier-merging must not touch them.
        parts = bullets
    else:
        parts = _SPLIT_RE.split(requirement)
        # Fold "with all the tracker" back into what it describes, before
        # anything treats it as work in its own right.
        parts = _merge_modifiers(parts)

    # BUG G: in "Support A, B, C and D" the verb governs every item. Splitting
    # on the separators attaches it only to the first, leaving the rest as bare
    # nouns ("communication history"), which then produce "I want communication
    # history". Carry the leading verb into the siblings.
    shared_verb = ""
    if len(parts) > 1:
        first_words = _clean_clause(parts[0]).split()
        if first_words and has_action_intent(first_words[0]):
            shared_verb = first_words[0]

    # A comma list ("A, B, C and D") is a real enumeration, so its items are
    # separate capabilities even when they are bare nouns. A lone "and" with no
    # commas usually joins parts of one phrase — "the pickup and drop-off
    # addresses" is one capability, not two.
    is_comma_list = requirement.count(",") >= 1 or len(bullets) >= 2

    clauses: list[str] = []
    seen: set[str] = set()
    for index, part in enumerate(parts):
        cleaned = _clean_clause(part)
        words = cleaned.split()
        if not cleaned:
            continue

        # A leading "when …" / "if …" sets a condition for the clause that
        # follows; it is context, not a capability of its own.
        if not clauses and _CONDITION_RE.match(cleaned):
            continue

        # A fragment that neither starts with a verb nor belongs to a comma
        # list is a continuation of the previous capability, not a new one.
        if clauses and not starts_with_verb(cleaned) and not is_comma_list:
            clauses[-1] = f"{clauses[-1]} and {cleaned}"
            continue

        if len(cleaned) < 4 or (len(words) < 2 and not is_comma_list):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        # The opening clause always counts; later ones must earn their place.
        if index > 0 or clauses:
            if key.startswith(_EXPLANATORY_PREFIXES):
                # Everything after "because …" belongs to the explanation, not
                # the request — including fragments the comma-and-"and" split
                # detached from it. Without this, "because they have to export
                # the data and add it up in a spreadsheet" contributes
                # "add it up in a spreadsheet" as a second capability.
                break
            if len(words) > _MAX_NOUN_PHRASE_WORDS and not has_action_intent(cleaned):
                continue
        seen.add(key)
        if (
            shared_verb
            and index > 0
            and len(words) <= 3
            and not cleaned.lower().startswith(shared_verb.lower())
        ):
            # Lowercase the joined-on word, unless it is an acronym: carrying
            # the verb into "SMS notifications" produced "send sMS
            # notifications", which then reached Jira as a Story title.
            head = cleaned.split(maxsplit=1)[0]
            tail = (
                cleaned
                if (len(head) > 1 and head[:2].isupper())
                else (cleaned[:1].lower() + cleaned[1:])
            )
            cleaned = f"{shared_verb.lower()} {tail}"
        clauses.append(cleaned)
    return clauses or [_clean_clause(requirement) or requirement.strip()]


def _size_for(count: int) -> Classification:
    """The one place the Small/Medium/Large boundaries are defined."""
    if count <= 1:
        return Classification.SMALL
    if count <= 4:
        return Classification.MEDIUM
    return Classification.LARGE


def classify(requirement: str) -> Classification:
    """Size a requirement from the number of distinct capabilities it asks for."""
    return _size_for(len(split_capabilities(requirement)))


def truncate(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Trim to ``limit`` on a word boundary, marking the cut with an ellipsis.

    Jira's own limit is 255 for every issue type. The previous code used three
    different magic numbers (80, 60, 120), none of them Jira's, and cut
    mid-word with nothing to show it had happened.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


_FALLBACK_TITLE = "Requirement"


def _flatten_list_markers(text: str) -> str:
    """Turn a bullet list into a readable clause for a one-line summary.

    An Epic built from a bullet list used to be summarised
    "Add: - driver shift roster - fuel spend by depot - tyre replacement log",
    with the markers still in it, because a Jira summary is one line and the
    list had none of its own punctuation.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    items = [re.sub(r"^(?:[-*\u2022]|\d+[.)])\s*", "", line) for line in lines if line]
    if not items:
        return text
    if len(items) == 1:
        # A single bullet still has a marker that does not belong in a summary.
        return items[0]
    head, rest = items[0], [i for i in items[1:] if i]
    if not rest:
        return head
    joined = ", ".join(rest[:-1])
    tail = f"{joined} and {rest[-1]}" if joined else rest[-1]
    # "Please add:" already ends in a colon; do not add a second separator.
    return f"{head.rstrip(':')}: {tail}" if head.endswith(":") else f"{head}, {tail}"


def _titleise(clause: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """A capability phrase fit for a Jira summary.

    Normalises first, so requester framing ("We would like ...") and modal
    padding ("to be able to") never reach the title.
    """
    title = truncate(normalise_capability(_flatten_list_markers(clause)), limit)
    title = (title[:1].upper() + title[1:]) if title else ""
    # The schema demands at least 3 characters. Degenerate input ("to to to")
    # can normalise down to almost nothing, and a crash there would take down
    # a whole run over a nonsense requirement.
    return title if len(title) >= 3 else _FALLBACK_TITLE


def _heuristic_subtasks(capability: str) -> list[Subtask]:
    """Three delivery-shaped subtasks per capability, described by behaviour."""
    return [
        Subtask(
            title=truncate(f"Define the rules for: {_titleise(capability)}"),
            description=(
                f"Agree and record the functional rules and edge cases for "
                f'"{capability}" with the product owner, using only what the '
                f"requirement states."
            ),
            expected_outcome=(
                "An agreed, written set of rules and edge cases that the "
                "implementation and tests can both be checked against."
            ),
            dependencies=NO_DEPENDENCIES,
            completion_criteria=(
                "The product owner has confirmed the recorded rules cover the "
                "capability with no open questions."
            ),
        ),
        Subtask(
            title=truncate(f"Implement the behaviour for: {_titleise(capability)}"),
            description=(
                f'Build the behaviour described by "{capability}" according to '
                f"the agreed rules."
            ),
            expected_outcome=(
                "The capability behaves as described for the stated users under "
                "normal and error conditions."
            ),
            dependencies="Depends on the agreed rules for this capability.",
            completion_criteria=(
                "The behaviour is demonstrable end to end and matches every " "agreed rule."
            ),
        ),
        Subtask(
            title=truncate(f"Verify and hand over: {_titleise(capability)}"),
            description=(
                f'Validate "{capability}" against the story acceptance criteria '
                f"and hand the capability over for review."
            ),
            expected_outcome=(
                "Evidence that each acceptance criterion for this capability is "
                "met, ready for sign-off."
            ),
            dependencies="Depends on the implemented behaviour for this capability.",
            completion_criteria=("All acceptance criteria pass and the reviewer has signed off."),
        ),
    ]


# Benefit phrasing keyed off the verb. Still generic — only the model can state
# real business value — but it beats one fixed sentence on every story.
_BENEFIT_BY_VERB = {
    "see": "I have the information in front of me when I need it",
    "view": "I have the information in front of me when I need it",
    "track": "I can see the current position without asking anyone",
    "export": "I can work with the data outside this system",
    "alert": "the right person hears about it without being chased",
    "notify": "the right person hears about it without being chased",
    "send": "the message reaches the recipient without manual effort",
    "assign": "work reaches the right person without a hand-off step",
    "report": "the problem is recorded the moment it is noticed",
    "update": "the record stays accurate without going through support",
    "upload": "the evidence is attached at the point it is captured",
    "download": "I keep a copy without asking anyone for it",
    "approve": "the decision is recorded and the work can move on",
    "cancel": "I can undo a mistake without contacting support",
    "filter": "I can find what I need in a long list",
    "mark": "the status reflects reality as soon as it changes",
}
_DEFAULT_BENEFIT = "I can do this myself instead of asking someone else"


def _article_for(noun: str) -> str:
    """ "a" or "an", chosen by how the word is said rather than how it is spelt.

    Plain vowel-letter matching gets the common fleet actors wrong both ways:
    "an operations manager" is right, but so is "a user" — and "user" starts
    with a vowel letter.
    """
    word = (noun or "").strip().split()[0].lower() if (noun or "").strip() else ""
    if not word:
        return "a"
    # Sounded as a consonant despite the vowel letter: user, unit, European…
    if word.startswith(("eu", "uni", "use", "user", "usu", "one")):
        return "a"
    # Sounded as a vowel despite the consonant letter: hour, honest…
    if word.startswith(("hour", "honest", "heir")):
        return "an"
    return "an" if word[0] in "aeiou" else "a"


def _benefit(action: str) -> str:
    first = action.strip().split()[0].lower() if action.strip() else ""
    return _BENEFIT_BY_VERB.get(first, _DEFAULT_BENEFIT)


def _heuristic_story(capability: str, fallback_actor: str = "") -> Story:
    raw_actor, raw_action = extract_actor(capability)
    # A list usually names its actor once: "Let dispatchers assign a job, and
    # notify the dispatcher when it is declined." The second clause has no
    # actor of its own, and a reader carries the first one across. Do the same
    # before giving up on "user of this product".
    actor = fallback_actor if raw_actor == _GENERIC_ACTOR and fallback_actor else raw_actor
    action = to_infinitive(raw_action)

    # "I want to <action>" only reads as English when <action> is a verb
    # phrase. When it is not — "the tyre log needs a monthly summary" — the
    # template produced "As a user of this product, I want to the tyre log
    # needs a monthly summary", which is what a reader sees and judges the
    # whole agent by. State the capability instead of forcing it into a shape
    # it does not fit.
    phrasable = bool(action) and starts_with_verb(action)
    if phrasable:
        statement = (
            f"As {_article_for(actor)} {actor}, I want to {action}, so that {_benefit(action)}."
        )
        value = (
            f"Gives the {actor} direct access to {action}, which the requirement "
            f"asks for explicitly."
        )
    else:
        subject = action or capability
        # The schema requires "I want" and "so that", so the shape is fixed —
        # what changes is that the requirement is quoted as a statement rather
        # than jammed in after "I want to".
        # The schema requires "I want to <action>" or "I want a/the <thing>",
        # so the capability is quoted as a thing rather than jammed in after
        # "I want to".
        statement = (
            f"As {_article_for(actor)} {actor}, I want the capability described "
            f'as "{subject}", so that the stated outcome is delivered.'
        )
        value = f"Delivers what the requirement states for the {actor}: {subject}."

    return Story(
        title=_titleise(action or capability),
        user_story_statement=statement,
        description=(f"Deliver the capability described in the requirement as: {capability}."),
        business_value=value,
        priority=Priority.MEDIUM,
        estimated_complexity=Complexity.MEDIUM,
        dependencies=NO_DEPENDENCIES,
        acceptance_criteria=[
            f'The behaviour described as "{capability}" is available to the intended users.',
            "The behaviour matches the rules agreed with the product owner.",
            "Error and edge cases behave as agreed rather than failing silently.",
        ],
        subtasks=_heuristic_subtasks(capability),
    )


def extract_exclusions(text: str) -> tuple[str, list[str]]:
    """Split the "ruled out" section off the requirement.

    Returns ``(requirement_without_it, excluded_phrases)``. The section has to
    survive :func:`requirement_core` to get here — it is a decision about the
    work, not discussion — but it must never be decomposed, or "SMS" becomes a
    Story called "SMS".
    """
    from src.context.ingest import HEADING_EXCLUSIONS

    marker = f"## {HEADING_EXCLUSIONS}"
    if marker not in (text or ""):
        return text, []
    before, _, after = text.partition(marker)
    rest = re.split(r"(?m)^## ", after, maxsplit=1)
    body = rest[0]
    remainder = f"## {rest[1]}" if len(rest) > 1 else ""
    phrases = [
        line.lstrip("- ").strip().lower()
        for line in body.splitlines()
        if line.strip().startswith("-")
    ]
    return (before + remainder).strip(), [p for p in phrases if p]


def drop_excluded(capabilities: list[str], exclusions: list[str]) -> tuple[list[str], list[str]]:
    """Remove capabilities a comment ruled out. Returns ``(kept, dropped)``.

    Matching is word containment, not equality: a comment says "SMS", the
    capability reads "Send SMS notifications to customers". Every word of the
    exclusion must appear, so "email" does not knock out "email and SMS".
    """
    kept: list[str] = []
    dropped: list[str] = []
    for capability in capabilities:
        words = set(re.findall(r"[a-z0-9]+", capability.lower()))
        if any(
            phrase and set(re.findall(r"[a-z0-9]+", phrase)).issubset(words)
            for phrase in exclusions
        ):
            dropped.append(capability)
        else:
            kept.append(capability)
    # Never empty the breakdown: if everything matched, the exclusion was too
    # broad to trust and a human should decide rather than get nothing.
    return (kept, dropped) if kept else (capabilities, [])


def heuristic_breakdown(requirement: str, context: ProjectContext) -> WorkBreakdown:
    """Build a schema-valid breakdown without a model.

    Faithful but generic: it restates the capabilities the requirement asks for
    and never invents business rules, roles, or dates.
    """
    # Everything below turns prose into ticket wording. The model can be told in
    # a heading that comments are only context; this path cannot read that, so it
    # is given the requirement sections alone, with the headings removed.
    requirement, exclusions = extract_exclusions(requirement_core(requirement))
    requirement = strip_section_headings(requirement)
    capabilities, dropped = drop_excluded(split_capabilities(requirement), exclusions)
    if dropped:
        logger.info(f"Ruled out in discussion, so not built: {dropped}")
    # Size what is actually being built. Sizing the original text counted work
    # that was ruled out, so excluding two of three capabilities still produced
    # an Epic over a single Story.
    classification = _size_for(len(capabilities))
    # Carry the most recently named actor across clauses that name none.
    stories: list[Story] = []
    seen_actor = ""
    for cap in capabilities:
        story = _heuristic_story(cap, seen_actor)
        named, _ = extract_actor(cap)
        if named != _GENERIC_ACTOR:
            seen_actor = named
        stories.append(story)

    if classification is Classification.SMALL:
        return WorkBreakdown(
            classification=classification,
            analysis=(
                "The requirement asks for one focused capability, so it is sized "
                "as Small and produces a single Story with its subtasks."
            ),
            stories=stories[:1],
        )

    scope_label = context.name or "the configured project"
    # Built from the capabilities that survived, not from the original text. An
    # Epic titled "Send email and SMS notifications" with no SMS Story under it
    # tells a reader the work is covered when it was deliberately dropped.
    epic_text = "; ".join(capabilities) if dropped else requirement.strip()
    epic = Epic(
        business_objective=(
            f"Deliver the capabilities described in the requirement for {scope_label}: "
            f"{truncate(epic_text, 400)}"
        ),
        scope=capabilities,
        # What was ruled out is known here, and this is the field for it.
        out_of_scope=dropped or [NOT_SPECIFIED],
        priority=Priority.MEDIUM,
        acceptance_criteria=[
            "Every capability listed in scope is delivered and demonstrable.",
            "Each related user story has passed its own acceptance criteria.",
        ],
        jira_summary=_titleise(epic_text) or "Requirement decomposition",
    )
    return WorkBreakdown(
        classification=classification,
        analysis=(
            f"The requirement asks for {len(capabilities)} distinct capabilities, "
            f"so it is sized as {classification.value} and produces an Epic with "
            f"one Story per capability."
        ),
        epic=epic,
        stories=stories,
    )


# --------------------------------------------------------------------------
# Public entry points used by the tools layer
# --------------------------------------------------------------------------


def _named(exc: Exception, step: str) -> DecompositionError:
    """One error type for every model failure when the model is required, so
    the tools layer catches all of them — a gateway timeout used to escape its
    ``except (DecompositionError, ValidationError)`` and crash the activity."""
    if isinstance(exc, DecompositionError):
        return exc
    named = DecompositionError(f"AI model {step} failed — {type(exc).__name__}: {exc}")
    named.__cause__ = exc
    return named


async def triage(requirement: str, context: ProjectContext) -> tuple[ValidationVerdict, str]:
    """Run LLM triage, degrading to 'proceed' when the gateway is unavailable."""
    try:
        return await llm_triage(requirement, context), GENERATOR_LLM
    except Exception as exc:  # gateway down, bad JSON, unexpected verdict
        if llm_required():
            raise _named(exc, "triage")
        logger.warning(f"LLM triage unavailable, using deterministic gate only: {exc}")
        return ValidationVerdict(status=ResultStatus.READY_FOR_JIRA), GENERATOR_HEURISTIC


async def build_breakdown(requirement: str, context: ProjectContext) -> tuple[WorkBreakdown, str]:
    """Produce a validated breakdown from the model — or nothing at all.

    The heuristic fallback used to run when the model failed. Its tickets were
    boilerplate unrelated to the requirement ("…matches the rules agreed with
    the product owner"), and could never pass the self-review gate. The owner
    decided (2026-09-25): when the model cannot produce the breakdown, nothing
    is created and the reply says so — whatever ``LTW_REQUIRE_LLM`` says.
    """
    # Filenames the person wrote are theirs to keep; only invented ones are refused.
    with allow_source_files_from(requirement):
        try:
            return await llm_breakdown(requirement, context), GENERATOR_LLM
        except Exception as exc:
            raise _named(exc, "decomposition")
