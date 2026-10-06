# How the code works

Versions: router **0.1.26** · Work Breakdown **4.3.42** · Requirement Review **2.2.19** (29 Sep 2026).
Line numbers are for `shared/`; each agent's copy in `<agent>/src/shared/` is about 3 lines lower.

## 1. What this system does

A person writes `@Aetherion …` in a comment on a Jira ticket. One automated reply comes back under that
comment. Depending on what was asked, the system either turns the requirement into Jira tickets, reviews
the ticket and lists what is missing, explains the ticket, or lists its own commands. It reads everything
attached to the request first: the description, comments, Word/PDF/Excel files, transcripts and linked
Confluence pages.

## 2. The three agents

| Agent | Does | Is NOT allowed to |
|---|---|---|
| **Router** `jira-orchestration` (JiraOrchestration) | Screens the comment, decides the intent, runs one child, posts the one reply, sends the email | create tickets, or answer a comment twice |
| **Work Breakdown** `jira-task-creation` (JiraTaskCreation) | Turns a requirement into Epic → Stories → Sub-tasks. When `@Aetherion build` points at a ticket, that ticket itself becomes the Epic, Story, Task or Bug | post its own reply when called by the router; write outside `LTW_ALLOWED_PROJECT_KEYS` (FL, BGV) |
| **Requirement Review** `jira-requirement-review` (JiraRequirementReview) | Finds gaps, ambiguities and missing acceptance criteria; gives a 1–5 readiness score | create or edit tickets. It *can* post a comment when run on its own, but the router always sends `post_to_jira: False` (`jira-orchestration/src/routing/catalog.py:123`) |

## 3. End to end: a comment typed → a reply posted

1. **Jira webhook** "Aetherion Jira Test" sends a *signed* event to `wh.sbox.aetherion.io`. A wrong secret is
   rejected silently (`401 Invalid signature`), and no run starts.
2. **Router entry**: `JiraOrchestration(payload)` at `jira-orchestration/src/agent/router_agent.py:37` →
   `run_router` at `router_flow.py:50`.
3. **Ingress**: `ingress_check` at `tools/tools.py:98` reads the issue and the comment. It returns
   `proceed`, `ignored` (silent: no keyword, our own reply, already answered, project not allowed, no
   issue, no comment) or `failed` (reply "Could not read this ticket").
4. **Classify**: Layer 1 matches a leading verb (`routing/classify.py:92`). Otherwise Layer 2 asks the model
   (`gpt-5.1`, temperature 0, 200 tokens, `tools/tools.py:245-278`) for scores over
   BUILD / REVIEW / QUESTION / CHATTER. If it is still unsure, Layer 3 replies "Which did you mean?".
5. **Dispatch**: `_dispatch_and_compose` (`router_flow.py:262`) starts **one Temporal child workflow** on
   that agent's task queue. Timeouts are 7 min for BUILD and 4 min for REVIEW. Retries: 3 attempts, 2 s → 10 s.
6. **Child works** and returns the shared `AgentResult` contract (`shared/contract.py`): outcome,
   headline, created[], duplicates[], questions[], sources_read[], sources_missing[], errors[].
7. **Email** (`send_outcome_email`, `router_flow.py:376`) only for clarification, duplicates and failed.
   The owner decided on no email for a successful build.
8. **Compose** (`routing/compose.py:186`) builds one reply from the contract. The layout depends on the
   outcome, never on which agent produced it.
9. **Post** (`post_reply`, `tools/tools.py:315`) replies *in the comment's thread* (undocumented `parentId`),
   then records the comment id as answered and adds the label `aetherion-processed`.

**Inside Work Breakdown**: read → triage (AI) → breakdown (AI) → checks (coverage of every line, every hard
fact, quality, self-review) → one regeneration → repair → duplicate check → create in Jira.

## 4. Folder map

| Folder | Holds |
|---|---|
| `jira-orchestration/src/routing/` | Pure router logic: ingress rules, classify, catalog, compose, settings |
| `jira-orchestration/src/agent/`, `tools/` | The Temporal workflow and the `@tool` activities that do I/O |
| `jira-task-creation/src/classification/` | Triage, breakdown, fact checks (`details.py`), self-review |
| `jira-task-creation/src/jira/` | Jira REST: reading, creating the hierarchy, duplicates, root conversion (`root.py`) |
| `jira-task-creation/src/context/` | Assembling the requirement from all sources, and the context budget |
| `jira-requirement-review/src/review/` | Review prompt, parser, grounding filters, readiness |
| `shared/` | Code every agent must behave identically on: attachments, Confluence, ADF, contract, mailer, rubric, keywords |
| `scripts/` | `sync_shared.py` (copies `shared/` into each agent) and `publish.py` |
| `docs/`, `tests.md`, `DemoTest.md` | How it works, live test plans |

**Why `shared/` exists:** the packer only ships an agent's own `src/`, so shared code is *copied* into
each agent by `scripts/sync_shared.py`, and a test fails if a copy drifts. **Rule:** code goes in `shared/`
only if two agents must give the same answer, for example reading the same PDF or signing replies the same way.
Always edit `shared/`, never a copy.

## 5. The reading pipeline

**Chain (Work Breakdown):** `read_jira_issue` (`jira-task-creation/src/tools/tools.py`) → `download_attachment`
(`src/jira/trigger.py:241`, follows redirects) → `extract()` (`shared/attachments.py:239`). That checks, in
order: supported type → size → `_extract_sync` in a thread → `fit_to_budget`. Then `assemble_requirement`
(`src/context/ingest.py`) builds one sectioned text for the model.

**We call no document library ourselves.** `shared/attachments.py:200` imports the platform's
`agent_lib.utils.file_utils` and dispatches on the extension (lines 202–222). No `fitz`, `pdfplumber` or
`openpyxl` import exists anywhere in our source.

| Format | Handled by | Library underneath (from `file_utils.pyi`) |
|---|---|---|
| .pdf | `file_utils.extract_text_from_pdf` | PyMuPDF (`fitz`) 1.26.4 |
| .docx | `extract_text_from_docx` | python-docx 1.2.0 |
| .doc | `extract_text_from_doc` | olefile |
| .xlsx / .xls | `extract_text_from_xlsx` / `_xls` | openpyxl 3.1.5 / xlrd |
| .pptx | `extract_text_from_pptx` | python-pptx 1.0.2 |
| .csv | `extract_text_from_csv` | stdlib csv |
| images | `extract_text_from_image` | platform **vision model** via the AI Gateway (not OCR) |
| .txt .md .log .rst | **our code**: UTF-8 decode | — |
| .json .jsonl | **our code**: parsed and pretty-printed | stdlib json |

`pdfplumber` 0.11.9 and `pypdfium2` 5.13.0 are installed as dependencies of other packages, but nothing
calls them.

**Caps (Work Breakdown):**

| Cap | Value | Where / env var |
|---|---|---|
| Files per ticket | 10 | `DEFAULT_MAX_FILES`, `shared/attachments.py:30` · `LTW_ATTACHMENT_MAX_FILES` |
| Bytes per file | 10 MB | `:29` · `LTW_ATTACHMENT_MAX_BYTES` |
| Characters per file | 20,000 | `:31` · `LTW_ATTACHMENT_MAX_CHARS` |
| Confluence pages / chars each | 5 / 20,000 | `shared/confluence_fetch.py:43-44` · `LTW_CONFLUENCE_MAX_PAGES`, `_MAX_CHARS` |
| Whole requirement sent to the model | 24,000 chars | `src/context/ingest.py:29` · `LTW_CONTEXT_MAX_CHARS` |
| Comments / history / work logs read | last 50 / 50 / 30 | `src/jira/trigger.py` |

Review uses fixed constants (`jira-requirement-review/src/config.py:23-46`): 10 files, 10 MB, 20,000 chars,
5 pages, 20 comments, a 200,000-character total. There are no `REVIEW_*` environment variables, even though
a docstring mentions them.

**A file that won't open:** there is never an error, only a note. The notes are "Unsupported attachment
type", "over the … KB limit; not read", "Could not read this attachment: <reason>" (which covers corrupt or
password-protected files) and "No text content found". The notes reach the reply as **"What I could not
read"**, each with what to do about it (`src/agent/delegated.py:233`).

**Middle-out truncation** (`fit_to_budget`, `shared/attachments.py:131`): a long document keeps **two
thirds from the start and one third from the end**, cut on paragraph breaks, with
`[... N characters omitted from the middle ...]` in between. Plain "first N characters" was rejected because
"the scope, the limits and the acceptance criteria are almost always at the end" (`:136-139`). When the
whole bundle is too big, whole sections are dropped in this order (`ingest.py:33-44`): work logs → history →
activity → links → ticket → comments → attachments → Confluence. The request itself is never dropped.

**Confluence:** links are found in the comment, the description, the summary and the ticket's "remote
links" (`fetch_remote_link_urls`, `confluence_fetch.py:317`). `/wiki/spaces/…/pages/<id>`,
`/wiki/display/…` and `/wiki/x/<shortlink>` are all recognised (`confluence_text.py:31-116`). **Only this
Atlassian site** is fetched (`_same_site`, `:72`); other links are reported as unread. A page is fetched in
storage format (`/wiki/rest/api/content/<id>?expand=body.storage`), stripped to text (`storage_to_text`),
then middle-out truncated. Its attachments are read like ticket attachments. A number that differs
between the ticket and a page is reported as "Conflicting information", and that page is left out.

## 6. Key design decisions

| Decision | Alternatives | Why this | What it costs |
|---|---|---|---|
| **One webhook → a router** | a webhook per agent | one comment → exactly one reply; two agents can't both answer | one extra hop; the router is a single point of failure |
| **Router posts the reply; children return a contract** | each child posts | "exactly one reply" is structural, and a new agent is a catalog entry, not a rewrite | children need a delegated mode |
| **Platform `file_utils`** | call PyMuPDF etc. directly | the platform owns PDF/Word/Excel/PowerPoint/images; we'd otherwise pin 6+ parsers in 3 agents | the parsing behaviour and versions come from the platform, not us |
| **Layer 1 verbs before the model** | model for everything | `build`/`review` are unambiguous, free and instant; the model only sees the unclear ones | verbs are a hand-kept list (`catalog.py:96,117`) |
| **BUILD 0.80, REVIEW 0.60** (`catalog.py:99,120`) | one threshold (0.75 default, `:49`) | a wrong BUILD creates tickets someone must delete; a wrong REVIEW is one comment (`:17-21`) | more "Which did you mean?" for unclear builds |
| **Child workflow, not HTTP** | HTTP between agents | Temporal gives retries, timeouts and a traceable parent/child run; the waiter must be the poster (`docs/AGENT_TO_AGENT.md`) | tied to Temporal; a child that never starts waits for the full timeout |
| **Idempotency on comment id** | per-ticket flag | one ticket can take many requests over its life | a redelivery is caught; two *different* comments at once are not serialised |
| **Pure logic apart from I/O** | logic inside tools | routing, compose, checks and root planning test with no Jira or model: 294 + 1081 + 236 tests | two layers to read |

**The duplicate layers:**
1. the router's answered-comments property (`aetherion-answered-comments`, key `event:comment_id`)
2. the child's `ltw-answered-comments`
3. an idempotency label `ltw-<hash>` on everything created (one label per root for life)
4. a comparison with existing tickets (word match 0.75, description 0.85, requirement 0.80, plus an AI "same work?" check)
5. comparison by title with a parent's existing children
6. the "already built" gate on a converted root

## 7. What is not built yet

- **No hard lock.** Each ticket's state (`aetherion-orchestration`) sequences its jobs — a duplicate request says "already running", a different one waits its turn — but two comments in the same second can both read "idle" before either writes. `README.md:17,35` still describe the old lock.
- **Prompt injection is untested** (`PHASES.md:342`). The comment goes straight into the classifier prompt (`tools/tools.py:260`). The limits on damage are a closed set of intents, schema validation of every AI answer, and the project allow-list.
- **`ORCH_IDEMPOTENCY_TTL_HOURS`** is defined and never used; the `aetherion-processed` label is stamped and never checked.
- **The D4 routing log** (why a comment went where) is not built.
- **`heuristic_breakdown`** (`decompose.py:1598`) is dead in production and used only by tests.
- **The Review agent runs SDK 0.0.67**; the other two run 0.0.83.
- **The graphql-core `<3.3` pin** is a workaround for the platform image (28 Sep 2026).
- **No cost metering**: model calls are not counted or billed per run.
- **No dedicated kill switch**, and no one-click rollback (see QA).
- **Root conversion** (a ticket becomes Epic/Story/Task/Bug) is observed working through the real code on BGV-45 and BGV-52, but not yet through a real `@Aetherion build` comment.
- **Stale docs**: `jira-task-creation/TESTING.md:632-652` and the lock mention in `README.md`.

## 8. Run and test

```bash
cd jira-task-creation            # or jira-orchestration / jira-requirement-review
uv run --offline pytest -o addopts="" -p no:warnings | tail -1
#  → 1081 passed, 1 skipped   (router: 294 passed · review: 236 passed)
python3 ../scripts/sync_shared.py --check          # shared/ copies match
UV_OFFLINE=1 uv run --offline python scripts/publish.py --bump patch --force
#  tests → lint → version → uv sync → aetherion publish   (router first if the contract changes)
```

A passing publish ends `Uploaded <agent> <version>`. That means **received, not live**. It is live when
Agent List shows the version and a real comment gets a reply. Live test plans: `tests.md`, `DemoTest.md`.
