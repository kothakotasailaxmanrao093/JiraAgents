# Aetherion Delivery Studio

A local web UI that takes a Jira card through the agents, end to end:

**Jira card → Requirement Review → your answers → (optional) Break down into Sub-tasks → Planning (technical design) → Coding (branch + draft pull request)**

Every agent run is a real Temporal workflow on this laptop's workers, linked to
the platform's Temporal UI (`wf.sbox.aetherion.io`, namespace `test`). Documents
open in the page. Nothing is written to Jira; Coding pushes a new branch and opens
a **draft** pull request, and nothing is merged.

## First time on a new laptop

```bash
git clone https://github.com/kothakotasailaxmanrao093/JiraAgents.git JiraAgent
cd JiraAgent
delivery-studio/setup.sh
```

`setup.sh` takes Planning and Coding from `AetherionAgents/asurint` (branches `sm/planning-agent`,
`sm/coding-agent`), installs every agent's packages, creates the Docker containers and copies the
settings templates. Then fill in `.env.local` and each agent's `.env` — get the values in person or
from a password manager, never in chat or git. `CODE_GRAPH_KEY` must be the same in
`jira-orchestration/.env` and `planning-agent/.env`; in `coding-agent/.env` set `CODING_OPEN_PR=true`
and `JENKINS_ENABLED=false`. Connect the VPN.

## Run it

From the `JiraAgent` folder:

```bash
delivery-studio/start.sh              # every agent + the UI, opens http://127.0.0.1:8780
delivery-studio/start.sh --no-open    # the same, without opening the browser
delivery-studio/start.sh --ui-only    # only the UI, when the agents already run
delivery-studio/stop.sh               # stop the UI and every agent
delivery-studio/stop.sh --ui-only     # stop only the UI
delivery-studio/logs.sh               # follow the logs (or: logs.sh planning / coding / review / studio)
```

Open a card directly: `http://127.0.0.1:8780/?card=KS-6`

### Before the first run

| Needed | Why | Check |
|---|---|---|
| `uv sync` in each agent folder | the workers run on each agent's `.venv` | `start.sh` says which one is missing |
| `.env.local` in `JiraAgent/` with `OPENAI_API_KEY` | model calls on a laptop | System page → OpenAI |
| Docker: `planning-neo4j`, `planning-s3` | Planning's storage | System page → Docker |
| **VPN** | Planning and Coding read the Keystone code on `github.asurint.com` | System page → GitHub Enterprise |

## The pages

| Page | What it shows |
|---|---|
| **Pipeline** | Load a card; run the whole flow or one step. **Break down** (Task Creation): preview the Sub-tasks (a Story) or Stories (an Epic) as a PDF, then create them in Jira — the card itself is never changed. Readiness score, open questions and an answers box; ranked services and the TDD; the branch and pull requests. Each step links to its Temporal workflow; a live log underneath. |
| **Runs** | Every run started here, with status, duration and workflow links. |
| **Card memory** | What Orchestration remembers about a card: questions with their answers, ranked services (encrypted at rest). |
| **System** | The local agents, Jira, GitHub Enterprise, Temporal, OpenAI and Docker; each agent's log. |
| **How it works** | The agents and how a card moves through them. |

## Readiness

The review's score counts the card's **open questions**, old ones included: 5/5 only when nothing is
open; one or two open questions cap it at 4/5; three or more at 3/5, below the 4/5 needed to plan.
Answer them in the Answers box (`Q1: …`) or as Jira comments, then review again.

## How it is built

```
delivery-studio/
├── setup.sh                       first time on a new laptop
├── start.sh  stop.sh  logs.sh     the commands
├── backend/
│   ├── main.py                    starts the server (FastAPI + uvicorn)
│   └── studio/
│       ├── config.py              paths, ports, links; loads the env files
│       ├── app.py                 the HTTP API (/api/…) and the page
│       ├── jobs.py                a run: review / plan / code / end to end, its log and workflows
│       ├── jira.py                reading cards (read only)
│       ├── documents.py           the PDFs made, the files uploaded
│       └── health.py              workers, Docker, Jira, GitHub, OpenAI; agent logs
└── frontend/
    ├── index.html
    ├── css/app.css
    └── js/app.js
```

The work itself is `scripts/local_run.py` — the same code as the terminal commands —
doing Orchestration's part around each agent: the card read from Jira, its memory
recalled and kept, its services ranked. The server runs on Orchestration's Python
(`jira-orchestration/.venv`) and needs no packages of its own. Run history is kept in
`local-output/studio/runs.json` (git-ignored). The studio is local only: it is never
published, and the agents' published code does not depend on it.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/runs` | start a run: `{"kind": "review"\|"breakdown"\|"plan"\|"code"\|"pipeline", "issue_key": "KS-6", …}` (`"create_in_jira": true` for a breakdown that creates) |
| `GET` | `/api/runs`, `/api/runs/{id}` | runs, one run with its log |
| `GET` | `/api/card/{key}`, `/api/cards` | a card, the project's recent cards |
| `GET` | `/api/memory/{key}` | the card's memory |
| `GET` | `/api/file?path=…` | a document (only from `local-output/` and `.local-uploads/`) |
| `POST` | `/api/upload` | upload a file for a run |
| `GET` | `/api/health`, `/api/logs/{agent}` | system status, an agent's log |
