"""Check the new behaviour from your laptop, one piece at a time.

Run from jira-task-creation/:

    uv run --offline python scripts/local_check.py reads BGV-69      # parallel reading, timed
    uv run --offline python scripts/local_check.py index BGV         # duplicate index, timed
    uv run --offline python scripts/local_check.py duplicates BGV "Send invoice reminders"
    uv run --offline python scripts/local_check.py pdf BGV-1         # builds the PDF, saves it here
    uv run --offline python scripts/local_check.py pdf BGV-1 --email # ...and emails it

Everything reads Jira only; nothing is created or changed there. ``pdf`` uses
a fixed sample breakdown — the AI gateway answers only inside the Aetherion
worker, so the AI steps are tested with a real "@Aetherion build" in Jira.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ.update({k: v for k, v in dotenv_values(".env").items() if v})
logging.disable(logging.CRITICAL)

from src.jira import api as jira  # noqa: E402
from src.jira import ticket_index  # noqa: E402
from src.tools import tools  # noqa: E402

SAMPLE = {
    "classification": "Medium",
    "analysis": "Invoicing and reminders are two related capabilities.",
    "epic": {
        "business_objective": "Bill every client monthly and chase unpaid invoices.",
        "scope": ["Monthly invoices", "Overdue reminders"],
        "out_of_scope": ["Online card payment"],
        "priority": "High",
        "acceptance_criteria": ["Every client with completed checks gets one invoice a month."],
        "jira_summary": "Automate Monthly Client Invoicing",
    },
    "stories": [
        {
            "title": title,
            "user_story_statement": f"As a billing officer, I want to {title.lower()}, so that clients pay on time.",
            "description": f"{title} for every client, as the requirement states.",
            "business_value": "Clients are billed and chased without manual work.",
            "priority": "High",
            "estimated_complexity": "Medium",
            "dependencies": "None",
            "acceptance_criteria": [
                "Given a client with 3 completed checks in June, when the run starts on 1 July, "
                "then one invoice lists those 3 checks",
                "Given an invoice unpaid 30 days after its due date, when the daily check runs, "
                "then one reminder email goes to every billing contact",
            ],
            "open_questions": ["What happens to a check completed on the last day of the month?"],
            "subtasks": [
                {
                    "title": f"{title}: prepare the data",
                    "description": "Collect the completed checks per client for the month.",
                    "expected_outcome": "A list of billable checks per client.",
                    "dependencies": "None",
                    "completion_criteria": "For a client with 3 checks in June, the list shows exactly those 3.",
                },
                {
                    "title": f"{title}: deliver the result",
                    "description": "Produce and send the output to every billing contact.",
                    "expected_outcome": "The client receives it.",
                    "dependencies": "None",
                    "completion_criteria": "On 1 July every billing contact of the test client receives one email.",
                },
            ],
        }
        for title in ("Generate Monthly Invoices", "Send Overdue Reminders")
    ],
}


def _client():
    base, email, token = jira.jira_creds()
    return base, jira.jira_client(base, email, token)


async def reads(key: str) -> None:
    for run in (1, 2):
        start = time.perf_counter()
        source = await tools.read_jira_issue(key, False, False, "")
        took = time.perf_counter() - start
        files = [a["filename"] for a in source.get("attachments") or []]
        pages = [p.get("title") for p in source.get("confluence_pages") or []]
        print(f"run {run}: read {key} in {took:.2f}s | attachments {files} | Confluence {pages}")


async def index(project: str) -> None:
    base, client = _client()
    async with client:
        for label in ("first build (reads every ticket)", "next build (only what changed)"):
            start = time.perf_counter()
            idx = await ticket_index.up_to_date(client, project, base)
            print(f"{label}: {time.perf_counter() - start:.2f}s — {len(idx.issues)} tickets indexed")


async def duplicates(project: str, title: str) -> None:
    base, client = _client()
    async with client:
        idx = await ticket_index.up_to_date(client, project, base)
    found = idx.candidates([title], title)
    report = jira.build_overlap_report([title], found, base_url=base, requirement=title)
    near = jira.find_near_misses([title], found, base_url=base)
    print(f"{len(idx.issues)} tickets in {project}; {len(found)} share a word with {title!r}")
    for m in report.matches:
        print(f"  DUPLICATE  {m.existing_key}  {m.existing_summary!r}  score {m.score} ({m.matched_on})")
    for m in near:
        print(f"  CLOSE      {m.existing_key}  {m.existing_summary!r}  score {m.score} (the AI would be asked)")
    if not (report.matches or near):
        print("  nothing matches — this would be built")


async def pdf(key: str, email: bool) -> None:
    from src.document import breakdown_pdf
    from src.models.schemas import WorkBreakdown

    model = WorkBreakdown.model_validate(SAMPLE)
    doc = breakdown_pdf.BreakdownDocument(
        issue_key=key,
        issue_summary="Local check — sample breakdown",
        project_key=key.split("-")[0],
        generated_at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        agent_version="local",
        breakdown=model,
        sources_read=[f"{key} description"],
        requirement_lines=["Unpaid invoices 30 days after the due date get one reminder."],
    )
    name = breakdown_pdf.file_name(key, model)
    Path(name).write_bytes(breakdown_pdf.render_pdf(doc))
    print(f"saved {ROOT / name} — open it to review the layout")
    if email:
        result = await tools.generate_breakdown_pdf(
            {
                "breakdown": SAMPLE,
                "requirement": "Local check.",
                "request": "- Unpaid invoices 30 days after the due date get one reminder.",
                "issue_key": key,
                "issue_summary": "Local check — sample breakdown (nothing was created)",
                "project_key": key.split("-")[0],
                "agent_version": "local",
            }
        )
        print(f"email: {result['status']} → {result.get('emailed_to') or result.get('reason')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="check", required=True)
    sub.add_parser("reads").add_argument("key")
    sub.add_parser("index").add_argument("project")
    d = sub.add_parser("duplicates")
    d.add_argument("project")
    d.add_argument("title")
    p = sub.add_parser("pdf")
    p.add_argument("key")
    p.add_argument("--email", action="store_true")
    args = parser.parse_args()
    if args.check == "reads":
        asyncio.run(reads(args.key))
    elif args.check == "index":
        asyncio.run(index(args.project))
    elif args.check == "duplicates":
        asyncio.run(duplicates(args.project, args.title))
    else:
        asyncio.run(pdf(args.key, args.email))


if __name__ == "__main__":
    main()
