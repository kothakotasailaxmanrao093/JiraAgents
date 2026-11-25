"""Run Review, Planning and Coding by hand on this laptop (2026-11-03).

No Orchestration agent and no platform: each agent runs on its local worker
(start them first with scripts/run_local.sh), and this script does what
Orchestration would around it — read the card from Jira, recall and keep the
card's memory, rank its services — with Orchestration's own code. The memory
is the local file of scripts/local_memsy.py, never the platform's Memsy.
Nothing is written to Jira; PDFs go to local-output/, earlier versions kept.

Run with Orchestration's Python, from the JiraAgent folder:
    jira-orchestration/.venv/bin/python scripts/local_run.py review BTS-60
    jira-orchestration/.venv/bin/python scripts/local_run.py plan   BTS-60 [--review <review.pdf>]
    jira-orchestration/.venv/bin/python scripts/local_run.py code   <BTS-60-TDD-v1.pdf>
    jira-orchestration/.venv/bin/python scripts/local_run.py memory BTS-60

Answers to the review's questions: typed on the local form ("Q3: ..."), kept in
local-output/answers/<card>.json and given to the memory as the card's
comments are — nothing is posted to Jira. Jira comments on the card count too,
as in the published flow.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "local-output"
ORCH = ROOT / "jira-orchestration"

# The launcher's order: .env.local first, then the agent's own .env (the shell wins).
load_dotenv(ROOT / ".env.local")
load_dotenv(ORCH / ".env")
os.environ.setdefault("AETHERION_LOCAL_RUN", "1")
sys.path[:0] = [str(ORCH / "src"), str(ROOT / "scripts")]

import local_infra  # noqa: E402
import local_memsy  # noqa: E402
from local_worker import local_queue  # noqa: E402

# Local storage (the Docker RustFS), where Planning keeps its TDD PDFs.
os.environ.update(local_infra.settings({}, local_infra.container_env(local_infra.STORAGE_CONTAINER)))

from agent import handoff_flow  # noqa: E402
from memory_service import cards, ranking  # noqa: E402
from routing import handoff  # noqa: E402
from routing import pipeline as rules  # noqa: E402
from routing import settings  # noqa: E402
from shared.contract import AgentResult  # noqa: E402
from tools import memory_tools, org_repos, pipeline_tools  # noqa: E402

MEMSY = local_memsy.install()

# Who wants to know when an agent's run starts and ends (the local studio:
# studio/server.py shows each one with its Temporal link). Called as
# observer(event, workflow, agent_dir, workflow_id, queue, result).
RUN_OBSERVER: ContextVar = ContextVar("local_run_observer", default=None)


def _tell(event: str, *args) -> None:
    observer = RUN_OBSERVER.get()
    if observer is not None:
        observer(event, *args)


def _fn(tool):
    return getattr(tool, "__wrapped__", tool)


async def _memory_tool(name: str, *args):
    """handoff_flow's Run: Orchestration's memory tools, called in this process."""
    return await _fn(getattr(memory_tools, name))(*args)


async def _start(workflow: str, agent_dir: str, payload: dict) -> dict:
    """One run of an agent on its local worker, as Orchestration starts one."""
    from aetherion_sdk.runtime import AetherionRuntime

    client = await AetherionRuntime().connect()
    queue = local_queue(agent_dir)
    workflow_id = f"local-{agent_dir}-{int(time.time())}"
    handle = await client.start_workflow(workflow, payload, id=workflow_id, task_queue=queue)
    print(f"  {workflow} started on {queue}; waiting…", flush=True)
    _tell("started", workflow, agent_dir, workflow_id, queue, None)
    try:
        out = await handle.result()
    except Exception as error:
        _tell("failed", workflow, agent_dir, workflow_id, queue, str(error))
        raise
    _tell("finished", workflow, agent_dir, workflow_id, queue, out)
    # An agent may answer with a list (its result, then its documents):
    # Orchestration reads the first item (command_flow._call), and so does this.
    if isinstance(out, list):
        out = out[0] if out else {}
    return out if isinstance(out, dict) else {}


def _open_in_order(questions: list[dict]) -> list[dict]:
    """The open questions as the report numbers them: Q1, Q2, … in order. The
    memory keeps its own ids (an answer stays linked to its question), so the
    report's numbers are translated back by ``_real_numbers`` (2026-11-05)."""
    return [q for q in questions if not q.get("answers")]


async def _real_numbers(issue_key: str, typed: str) -> str:
    """"Q1: …" typed against the last report's numbering, as the memory's own
    question id ("Q2: …" when Q2 is the first one still open)."""
    if not re.search(r"(?m)^\s*[QqAa]\d+\s*[:.)-]", typed or ""):
        return typed
    _, snapshot = await cards.load(issue_key)
    shown = _open_in_order(snapshot.get("questions") or [])

    def real(match: re.Match) -> str:
        n = int(match.group(2))
        if 1 <= n <= len(shown):
            return f"{match.group(1)}{shown[n - 1]['id']}{match.group(3)}"
        return match.group(0)

    return re.sub(r"(?m)^(\s*)[QqAa](\d+)(\s*[:.)-])", real, typed)


def form_answers(issue_key: str, typed: str = "") -> list[dict]:
    """Every answer typed on the form for this card, as the memory reads a
    comment: {"id", "text"}, one per "Qn:" block. New ones are kept with the
    earlier ones in local-output/answers/<card>.json; an id is the text's own
    hash, so the same answer typed twice counts once."""
    path = OUT / "answers" / f"{issue_key.upper()}.json"
    kept = json.loads(path.read_text()) if path.is_file() else []
    numbered = re.search(r"(?m)^\s*[QqAa]\d+\s*[:.)-]", typed or "")
    # Numbered answers one per block; a document without numbers is one block,
    # matched to the questions by the memory's model.
    blocks = [b.strip() for b in (re.split(r"\n(?=\s*[QqAa]\d+\s*[:.)-])", typed or "")
                                  if numbered else [typed or ""]) if b.strip()]
    for text in blocks:
        entry = {"id": "local-form-" + hashlib.sha256(text.encode()).hexdigest()[:12],
                 "text": text}
        if entry["id"] not in {k["id"] for k in kept}:
            kept.append(entry)
    if blocks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept, indent=1, ensure_ascii=False))
    return kept


async def _card(issue_key: str, typed_answers: str = "") -> tuple[dict, dict]:
    """(the card read from Jira, with the form's answers among its comments;
    the card as Planning takes it)."""
    bundle = await _fn(pipeline_tools.story_bundle)(issue_key.upper(), [])
    if not bundle.get("ok"):
        raise SystemExit(f"Could not read {issue_key} from Jira: {bundle.get('error')}")
    answers = form_answers(issue_key, typed_answers)
    bundle["people_comments"] = list(bundle.get("people_comments") or []) + answers
    bundle["comments"] = list(bundle.get("comments") or []) + [a["text"] for a in answers]
    return bundle, handoff.card(bundle, list(bundle.get("files") or []))


def _next_version(folder: Path, stem: str) -> tuple[Path, Path | None]:
    """(the next version's path, the latest existing one or None)."""
    folder.mkdir(parents=True, exist_ok=True)
    found = sorted(
        (int(m.group(1)), p) for p in folder.glob(f"{stem}-v*.pdf")
        if (m := re.search(r"-v(\d+)\.pdf$", p.name))
    )
    n = found[-1][0] + 1 if found else 1
    return folder / f"{stem}-v{n}.pdf", (found[-1][1] if found else None)


def _pdf(html_text: str, out: Path) -> None:
    """HTML to PDF with Planning's own renderer (xhtml2pdf is in its environment)."""
    src = out.with_suffix(".html")
    src.write_text(html_text)
    code = ("import sys; from xhtml2pdf import pisa; "
            "pisa.CreatePDF(open(sys.argv[1]).read(), dest=open(sys.argv[2], 'wb'))")
    subprocess.run([str(ROOT / "planning-agent/.venv/bin/python"), "-c", code, str(src), str(out)],
                   check=True)
    src.unlink()


# --- review -------------------------------------------------------------------------------


def _answer_text(answer) -> str:
    """An answer as the memory gives it: its text (after a review) or {"text"} (stored)."""
    return str(answer.get("text", "") if isinstance(answer, dict) else answer)


def _review_html(issue_key: str, title: str, review: AgentResult, questions: list[dict],
                 ready: bool, score: int) -> str:
    e = html.escape
    # Only what is still needed; the answered ones are counted, not repeated
    # (KS-6, 2026-11-05: long pasted answers buried the open questions).
    rows = [
        f"<li><b>Q{n}</b> {e(q.get('question', ''))}</li>"
        for n, q in enumerate(_open_in_order(questions), start=1)
    ]
    answered = len(questions) - len(rows)
    done = (f"<p>{answered} earlier question(s) are already answered by the card, its "
            "comments or your answers.</p>" if answered else "")
    findings = "".join(
        f"<li>{e(str(f.get('description') or f.get('title') or ''))}</li>" for f in review.findings
    )
    missing = "".join(
        f"<li>{e(m.as_sentence())}</li>" for m in review.sources_missing
    )
    verdict = "Ready for planning" if ready else "Not ready for planning yet"
    held = (f" (the review alone: {review.readiness_score}/5; lowered while questions are open)"
            if score < review.readiness_score else "")
    return f"""<html><body style="font-family: Helvetica; font-size: 10pt">
<h1>{e(issue_key)} — Requirement review</h1><h3>{e(title)}</h3>
<p><b>{verdict}</b> — readiness {score}/5{held}.</p><p>{e(review.summary or '')}</p>
<h2>Findings</h2><ul>{findings or '<li>None</li>'}</ul>
<h2>Open questions (answer in the Answers box: "Q1: ...")</h2>
<ul>{''.join(rows) or '<li>None — nothing more is needed</li>'}</ul>{done}
<h2>Missing or unreadable</h2><ul>{missing or '<li>Nothing</li>'}</ul>
</body></html>"""


async def _document_text(key: str) -> str:
    """A file's text, read the way the agents read attachments."""
    from shared.attachments import extract

    path = _local_file(key)
    found = await extract(path.read_bytes(), path.name)
    if not found.text:
        print(f"  {path.name}: no text ({getattr(found, 'note', '')})")
    return found.text or ""


async def review(issue_key: str, answers: str = "", files: list[str] | None = None) -> dict:
    """The review with the card's memory; its PDF (only when something changed)
    and what it found. ``answers``: typed on the form, "Q1: ..." per question;
    ``files``: documents holding answers (the review's report, answered) —
    their text is given to the memory like a comment, which matches it to the
    open questions."""
    issue_key = issue_key.upper()
    for key in files or []:
        text = (await _document_text(key)).strip()
        if "Requirement Review Report" in text and not re.search(
            r"(?mi)^\s*(Q\d+|A\d+|Answer)\s*[:.)-]", text
        ):
            # The review's own report, with no answers written in it: its
            # findings are not answers (BTS-40, 2026-11-03).
            print(f"  {key.rsplit('/', 1)[-1]}: the review's report with no answers in it; not used")
            continue
        if text:
            answers = f"{answers}\n{text}".strip()
    bundle, card = await _card(issue_key, await _real_numbers(issue_key, answers))
    repos = await org_repos.planning_repos()
    recalled = await handoff_flow.recall(_memory_tool, issue_key, bundle, repos)
    print(f"  memory: {'recalled' if recalled else 'none yet'}")
    payload = {"issue_key": issue_key, "run_id": f"local-{int(time.time())}",
               "mode": "delegated", "post_to_jira": False}
    if recalled and recalled.get("document"):
        payload["memory_document"] = recalled["document"]
    result = AgentResult.from_dict(
        await _start("JiraRequirementReview", "jira-requirement-review", payload)
    )
    kept = None
    if recalled:
        kept = await handoff_flow.keep(_memory_tool, issue_key, recalled,
                                       rules.review_points(result), card["title"],
                                       result.summary)
    questions = (kept or {}).get("questions") or [
        {"id": f"Q{i}", "question": q, "answers": []}
        for i, q in enumerate(rules.review_points(result), start=1)
    ]
    still_open = [q for q in questions if not q.get("answers")]
    # Orchestration's own gate: the score alone; open questions go to Planning
    # as open (KS-6, 2026-11-04: all answered was this script's rule only).
    # Unanswered questions count against readiness, old ones included (2026-11-24):
    # the review only looks for what is NEW, so it alone could say 5/5 with 6 open.
    score = rules.readiness_with_open(result.readiness_score, len(still_open))
    ready = score >= (settings.tdd_min_readiness() or 1)
    page = _review_html(issue_key, card["title"], result, questions, ready, score)
    digest = hashlib.sha256(page.encode()).hexdigest()
    out, latest = _next_version(OUT / "requirement-review", f"{issue_key}-review")
    stamp = latest.with_suffix(".sha") if latest else None
    if stamp and stamp.is_file() and stamp.read_text() == digest:
        pdf, note = latest, f"nothing changed since {latest.name}: no new PDF"
    else:
        _pdf(page, out)
        out.with_suffix(".sha").write_text(digest)
        pdf, note = out, f"saved {out.name}"
    answered = len(questions) - len(still_open)
    message = (
        f"{issue_key}: readiness {score}/5"
        + (f" (review {result.readiness_score}/5, lowered: {len(still_open)} question(s) open)"
           if score < result.readiness_score else "")
        + f", {len(result.findings)} findings, "
        f"{answered} of {len(questions)} questions answered. "
        + ("READY FOR PLANNING." + (f" {len(still_open)} question(s) still open go to Planning as "
                                     "open; answer them in the Answers box to settle them."
                                     if still_open else "") if ready else
           f"NOT READY: {len(still_open)} question(s) open — answer them in the Answers box "
           '("Q1: ..."), then run the review again.')
    )
    return {"status": "success", "message": message, "ready": ready, "score": score,
            "review_score": result.readiness_score, "pdf": str(pdf),
            "note": note, "open_questions": [f"Q{n} {q.get('question')}" for n, q
                                             in enumerate(_open_in_order(questions), start=1)]}


# --- plan ---------------------------------------------------------------------------------


async def plan(
    issue_key: str, files: list[str] | str | None = None, only_services: str = ""
) -> dict:
    """The TDD from the card, its ranked services and its answered questions —
    with the review report and any other files (paths, or form uploads' keys)."""
    files = [files] if isinstance(files, str) else list(files or [])
    issue_key = issue_key.upper()
    bundle, card = await _card(issue_key)
    repos = await org_repos.planning_repos()
    kept = await handoff_flow.refresh(_memory_tool, issue_key, bundle, repos)
    if not kept:
        kept = await _fn(memory_tools.rank_card)(handoff.card_text(card), repos)
    services = ranking.in_order(list(kept.get("services") or []))
    wanted = [n.strip().lower() for n in re.split(r"[,\s]+", only_services or "") if n.strip()]
    if wanted:
        # The person names the services (a ranking can pick a look-alike repo:
        # "Client UI" -> the old keystone/client-ui, BTS-40, 2026-11-03).
        by_name = {s["service"].lower(): s for s in services}
        known = ranking.parse_repos(repos)
        services = []
        for rank, name in enumerate(wanted, start=1):
            found = by_name.get(name)
            if found is None and name in {k.lower() for k in known}:
                key = next(k for k in known if k.lower() == name)
                found = {"service": key, "repo": known[key][0], "branch": known[key][1],
                         "reason": "Named by the person who ran the plan.", "prs": []}
            if found is None:
                return {"status": "error", "message": f"No repository named {name!r} is known."}
            services.append({**found, "rank": rank})
    print("  services: " + (", ".join(f"{s['rank']}. {s['service']} ({s['repo']})"
                                      for s in services) or "none ranked"))
    if not services:
        return {"status": "error", "message": (
            "No services to design for: the repositories could not be listed"
            + ("" if repos.strip() else
               f" (the {settings.github_org() or 'GitHub'} organization's repositories were not "
               "readable — is the VPN on for github.asurint.com? — and ORCH_PLANNING_REPOS "
               "names none)")
            + ". Connect, then plan again.")}
    decisions = await _fn(memory_tools.decisions_in_force)(
        issue_key, [s["service"] for s in services])
    payload = {**handoff.payload(card, services, list(kept.get("questions") or [])),
               "issue_key": issue_key, "return_document": True,
               "decisions": list(decisions.get("decisions") or [])}
    attachments = []
    for item in files:
        key = item if item.startswith("local-uploads/") else str(Path(item).expanduser().resolve())
        name = key.rsplit("/", 1)[-1]
        review_like = "review" in name.lower()
        attachments.append({"key": key, "name": name, "description": (
            "The finalized requirement review: findings, questions and the answers given."
            if review_like else "A file given with the card for this design.")})
    if attachments:
        payload["attachments"] = attachments
    out = await _start("planning_agent", "planning-agent", payload)
    key = rules.tdd_key(out)
    if out.get("status") != "success" or not key:
        return {"status": "error", "message": f"Planning wrote no TDD: {out.get('message') or out}"}
    from common_lib.storage.storage_client import RetrievalMode, storage

    storage.init_client()
    data = storage.retrieve(os.environ["TENANT_ID"], key, RetrievalMode.FULL_OBJECT)
    dest, _ = _next_version(OUT / "planning-agent", f"{issue_key}-TDD")
    dest.write_bytes(data)
    # The design in the project's history, as the published flow keeps it
    # (command_flow): a later card about the same thing finds it (2026-11-24).
    version = re.search(r"-v(\d+)\.pdf$", dest.name).group(1)
    event_id, data = handoff.design_event(card, services, {"version": version, "url": str(dest)})
    await handoff_flow.remember(_memory_tool, issue_key, "design", data, event_id)
    message = f"{issue_key}: TDD {out.get('tdd_status')} — {dest.name}"
    if out.get("draft_reason"):
        message += f" (draft because: {out['draft_reason']})"
    return {"status": "success", "message": message, "pdf": str(dest),
            "services": [f"{s['rank']}. {s['service']} ({s['repo']})" for s in services]}


# --- code ---------------------------------------------------------------------------------


def _local_file(pdf: str) -> Path:
    """A path, or a form upload's key (local-uploads/<id>/<name>)."""
    if pdf.startswith("local-uploads/"):
        import local_files

        return local_files.folder() / pdf[len("local-uploads/"):]
    return Path(pdf).expanduser().resolve()


def task_of(pdf: str) -> dict:
    """The work order Planning keeps inside the PDF, read with Coding's pypdf."""
    code = ("import sys, json; from pypdf import PdfReader; "
            "m = PdfReader(sys.argv[1]).metadata or {}; print(m.get('/CodingTask') or '{}')")
    out = subprocess.run([str(ROOT / "coding-agent/.venv/bin/python"), "-c", code,
                          str(_local_file(pdf))], check=True, capture_output=True, text=True).stdout
    return json.loads(out or "{}")


# --- the branch Coding starts from (2026-11-25) ------------------------------------------
# "@Aetherion code" asks where the new branch is cut from; Coding (0.0.13+) always
# starts from the branch and commit in the TDD's work order. Another source branch
# is given to it by a copy of the TDD whose work order names that branch and its
# latest commit — the pull request then goes into that branch too.


def _github_get(path: str, params: dict | None = None):
    import httpx

    base = settings.github_api_url().rstrip("/")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(f"{base}{path}", params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def branches(repo: str, limit: int = 300) -> list[str]:
    """The repository's branch names (the default branch first)."""
    info = _github_get(f"/repos/{repo}")
    names: list[str] = []
    for page in range(1, limit // 100 + 1):
        got = _github_get(f"/repos/{repo}/branches", {"per_page": 100, "page": page})
        names += [b["name"] for b in got]
        if len(got) < 100:
            break
    default = info.get("default_branch") or ""
    return ([default] if default in names else []) + sorted(n for n in names if n != default)


def branch_head(repo: str, branch: str) -> str:
    """The latest commit on a branch."""
    from urllib.parse import quote

    return _github_get(f"/repos/{repo}/branches/{quote(branch, safe='')}")["commit"]["sha"]


def branch_exists(repo: str, branch: str) -> bool:
    """True or False from GitHub; anything else (no VPN, a refused token) raises."""
    import httpx

    try:
        branch_head(repo, branch)
        return True
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            return False
        raise


def from_branch(pdf: str, source: str) -> str:
    """A copy of the TDD whose work order starts every service from ``source``
    (at its latest commit); the path of the copy."""
    task = task_of(pdf)
    for svc in task.get("services") or []:
        svc["branch"] = source
        svc["commit"] = branch_head(svc["repo"], source)
    original = _local_file(pdf)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", source)
    copy = OUT / "planning-agent" / "from-branch" / f"{original.stem}-from-{safe}.pdf"
    copy.parent.mkdir(parents=True, exist_ok=True)
    code = ("import sys, json; from pypdf import PdfReader, PdfWriter; "
            "w = PdfWriter(clone_from=PdfReader(sys.argv[1])); "
            "w.add_metadata({'/CodingTask': sys.argv[3]}); w.write(sys.argv[2])")
    subprocess.run([str(ROOT / "coding-agent/.venv/bin/python"), "-c", code, str(original),
                    str(copy), json.dumps(task)], check=True)
    return str(copy)


def proposed_branch(task: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(task.get("ticket") or task.get("title") or "change")
                  .lower()).strip("-")[:40]
    return f"fix/{slug}-{int(time.time()) % 100000}"


async def code(pdf: str, branch_name: str = "", source_branch: str = "") -> dict:
    """The TDD implemented: a new branch ``branch_name`` per service, cut from
    ``source_branch`` (else the branch the TDD was written from), and a draft
    pull request into that same branch."""
    task = task_of(pdf)
    services = task.get("services") or []
    if not services:
        return {"status": "error", "message":
                "This PDF holds no work order (a TDD written before 2026-11-02?)."}
    branch = branch_name.strip() or proposed_branch(task)
    source = source_branch.strip()
    for svc in services:
        if branch_exists(svc["repo"], branch):
            return {"status": "error", "message":
                    f"A branch named {branch!r} already exists in {svc['repo']}: choose a new name."}
    if source and any(svc["branch"] != source for svc in services):
        for svc in services:
            if not branch_exists(svc["repo"], source):
                return {"status": "error", "message":
                        f"{svc['repo']} has no branch {source!r} to start from."}
        print(f"  starting from {source} (the TDD was written from "
              f"{', '.join(sorted({s['branch'] for s in services}))})")
        pdf = from_branch(pdf, source)
        task = task_of(pdf)
        services = task.get("services") or []
    if pdf.startswith("local-uploads/"):
        key = pdf
    else:
        from common_lib.storage.storage_client import storage

        storage.init_client()
        path = _local_file(pdf)
        _, key = storage.store_object(os.environ["TENANT_ID"],
                                      f"technical_designs/local/{path.name}",
                                      path.read_bytes(), "application/pdf")
    # Coding since 0.0.13 (2026-11-23) takes one branch name; each pull request
    # goes into the branch its TDD was written from (the work order's).
    payload = {"mode": "delegated", "uploaded_files": [key], "branch_name": branch,
               "team_id": os.environ.get("TENANT_ID", "")}
    out = await _start("coding-agent", "coding-agent", payload)
    pulls = rules.pull_requests(out)
    branches = rules.pushed_branches(out)
    # Each pull request in the project's history, as the published flow keeps it
    # (command_flow): the next card about the same thing is ranked with it, and
    # Planning reads it as a reference change (2026-11-24).
    card_key = str(task.get("ticket") or "").upper()
    if card_key:
        for pr in pulls:
            pr.setdefault("branch", branch)
        for event_id, data in handoff.pr_events({"pull_requests": pulls},
                                                str(task.get("title") or "")):
            await handoff_flow.remember(_memory_tool, card_key, "pr_opened", data, event_id)
    prs = [f"{pr['service']}: {pr['pr_url']}{' (draft)' if pr.get('draft') else ''}"
           for pr in pulls]
    pushed = [f"{b['service']}: {b['url']}" for b in branches]
    others = [f"{s['service']}: {s['reason']}" for s in out.get("skipped") or []]
    if out.get("message"):
        others.append(str(out["message"]))
    sources = ", ".join(sorted({s["branch"] for s in services}))
    message = (f"Branch {branch}, cut from {sources}; pull request into {sources}. "
               + ("; ".join(f"PR {p}" for p in prs) or "No pull request.")
               + (" Pushed: " + "; ".join(pushed) if pushed else "")
               + (" " + "; ".join(others) if others else ""))
    status = "success" if prs or pushed else out.get("status", "nothing_pushed")
    return {"status": status, "message": message, "pull_requests": prs,
            "pull_request_links": pulls, "branches": branches, "branch": branch,
            "source": sources,
            "services": [{"service": s["service"], "repo": s["repo"], "branch": s["branch"]}
                         for s in services]}


async def code_interactive(pdf: str) -> dict:
    """The terminal's form: the branch to start from and the new branch's name asked."""
    task = task_of(pdf)
    print(f"TDD: {task.get('title')} ({task.get('ticket')}, {task.get('status')})")
    for s in task.get("services") or []:
        print(f"  {s['service']}: {s['repo']} at {s['branch']} ({s['commit'][:12]})")
    first = (task.get("services") or [{}])[0].get("branch", "")
    target = input(f"Start from which branch (the pull request goes into it too)? [{first}]: ").strip()
    branch = input(f"New branch name [{proposed_branch(task)}]: ").strip()
    return await code(pdf, branch, target)


# --- break down (Task Creation) -----------------------------------------------------------


async def breakdown(issue_key: str, create: bool = False) -> dict:
    """The card split into work by Task Creation: a Story gets Sub-tasks, an Epic
    Stories (the card itself is never changed). ``create`` False: a preview — the
    breakdown as a PDF, nothing written to Jira. True: the issues are created."""
    issue_key = issue_key.upper()
    payload = {"issue_key": issue_key, "mode": "delegated", "force": True,
               "reply_on_ticket": "no", "create_in_jira": bool(create),
               "output": "create_in_jira" if create else "pdf_by_email"}
    started = time.time()
    out = await _start("JiraTaskCreation", "jira-task-creation", payload)
    result = AgentResult.from_dict(out)
    created = [{"key": c.key, "type": c.issue_type, "summary": c.summary, "url": c.url}
               for c in result.created]
    folder = ROOT / "docs-export" / "task-creation"
    pdfs = sorted((p for p in folder.glob(f"*{issue_key}*.pdf") if p.stat().st_mtime >= started - 5),
                  key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    ok = result.outcome.value in ("CREATED", "PLANNED", "REVIEWED") and not result.errors
    return {"status": "success" if ok else "error", "outcome": result.outcome.value,
            "message": f"{result.headline}. {result.summary}".strip(),
            "proposed": list(result.proposed), "created": created,
            "pdf": str(pdfs[0]) if pdfs else ""}


# --- memory -------------------------------------------------------------------------------


async def memory(issue_key: str) -> dict:
    """What the local memory holds for a card: its questions with their
    answers and its ranked services (decrypted with CODE_GRAPH_KEY)."""
    seq, snapshot = await cards.load(issue_key.upper())
    lines = [f"{issue_key.upper()} — snapshot {seq or 'none'} in {local_memsy.FILE}"]
    for q in snapshot.get("questions") or []:
        answers = " / ".join(_answer_text(a) for a in q.get("answers") or [])
        lines.append(f"  {q.get('id')} {q.get('question')}\n      -> {answers or '(open)'}")
    for s in ranking.in_order(list(snapshot.get("services") or [])):
        lines.append(f"  service {s.get('rank')}. {s.get('service')} ({s.get('repo')}): "
                     f"{s.get('reason')}")
    return {"status": "success", "message": "\n".join(lines)}


def main(argv: list[str]) -> None:
    if len(argv) < 2 or argv[0] not in ("review", "plan", "code", "memory"):
        raise SystemExit(__doc__)
    command, target = argv[0], argv[1]
    if command == "review":
        out = asyncio.run(review(target, argv[argv.index("--answers") + 1]
                                 if "--answers" in argv else ""))
    elif command == "plan":
        out = asyncio.run(plan(target, [argv[argv.index("--review") + 1]]
                                if "--review" in argv else []))
    elif command == "code":
        out = asyncio.run(code_interactive(target))
    else:
        out = asyncio.run(memory(target))
    print("\n" + out["message"])
    for key in ("pdf", "note"):
        if out.get(key):
            print(f"{key}: {out[key]}")
    for q in out.get("open_questions") or []:
        print(f"  open: {q}")


if __name__ == "__main__":
    main(sys.argv[1:])
