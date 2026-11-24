"""The HTTP API the page calls, and the page itself (frontend/)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, documents, health, jira, jobs

app = FastAPI(title="Aetherion Delivery Studio", docs_url=None, redoc_url=None)


class RunRequest(BaseModel):
    kind: str
    issue_key: str = ""
    answers: str = ""
    services: str = ""
    branch_name: str = ""
    files: list[str] = []
    plan_files: list[str] = []
    review_pdf: str = ""
    tdd_pdf: str = ""
    create_in_jira: bool = False


# --- runs -----------------------------------------------------------------------------


@app.post("/api/runs")
async def start_run(req: RunRequest) -> dict[str, Any]:
    kind = req.kind.strip().lower()
    if kind not in jobs.KINDS:
        raise HTTPException(400, "kind must be review, breakdown, plan, code or pipeline")
    key = req.issue_key.strip().upper()
    if kind != "code" and not key:
        raise HTTPException(400, "Give the card's key, e.g. KS-6.")
    if jobs.LOCK.locked():
        raise HTTPException(409, "Another run is in progress; wait for it to finish.")
    for name in ("review_pdf", "tdd_pdf"):
        value = getattr(req, name)
        if value and not documents.allowed(Path(value)):
            raise HTTPException(400, f"{name} is not a document this studio made")
    return jobs.start(kind, key, req.model_dump()).view()


@app.get("/api/runs")
async def list_runs() -> list[dict[str, Any]]:
    return jobs.all_runs()


@app.get("/api/runs/{job_id}")
async def get_run(job_id: str) -> dict[str, Any]:
    found = jobs.find(job_id)
    if found is None:
        raise HTTPException(404, "No such run")
    return found


# --- Jira and memory ------------------------------------------------------------------


@app.get("/api/card/{issue_key}")
async def get_card(issue_key: str) -> dict[str, Any]:
    try:
        return await jira.card(issue_key)
    except jira.NotFound as e:
        raise HTTPException(404, str(e)) from None
    except jira.JiraError as e:
        raise HTTPException(502, str(e)) from None


@app.get("/api/cards")
async def list_cards(project: str = "") -> list[dict[str, Any]]:
    return await jira.recent(project)


@app.get("/api/memory/{issue_key}")
async def get_memory(issue_key: str) -> dict[str, Any]:
    import local_run
    from memory_service import cards as card_memory
    from memory_service import ranking

    key = issue_key.strip().upper()
    seq, snapshot = await card_memory.load(key)
    questions = []
    for q in snapshot.get("questions") or []:
        answers = [local_run._answer_text(a) for a in q.get("answers") or []]
        questions.append(
            {"id": q.get("id"), "question": q.get("question"), "answers": answers,
             "open": not answers}
        )
    services = [
        {k: s.get(k) for k in ("rank", "service", "repo", "branch", "reason")}
        for s in ranking.in_order(list(snapshot.get("services") or []))
    ]
    return {"issue_key": key, "snapshot": seq, "questions": questions, "services": services,
            "encrypted": True}


# --- documents ------------------------------------------------------------------------


@app.get("/api/file")
async def get_file(path: str) -> FileResponse:
    p = Path(path)
    if not documents.allowed(p) or not p.is_file():
        raise HTTPException(404, "No such document")
    kind = "application/pdf" if p.suffix.lower() == ".pdf" else "application/octet-stream"
    return FileResponse(p, media_type=kind, filename=p.name, content_disposition_type="inline")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if len(data) > config.MAX_UPLOAD:
        raise HTTPException(413, "Files up to 25 MB")
    return documents.save_upload(file.filename or "upload", data, file.content_type or "")


# --- system ---------------------------------------------------------------------------


@app.get("/api/health")
async def get_health() -> dict[str, Any]:
    return await health.report(busy=jobs.LOCK.locked())


@app.get("/api/logs/{agent}")
async def get_log(agent: str, lines: int = 200) -> dict[str, Any]:
    if agent not in config.AGENTS:
        raise HTTPException(404, "No such agent")
    return {"lines": health.agent_log(agent, lines)}


app.mount("/", StaticFiles(directory=config.FRONTEND, html=True), name="frontend")
