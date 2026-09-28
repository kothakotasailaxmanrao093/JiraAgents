# How the Aetherion Jira agents work

Three agents on the Aetherion platform turn a Jira comment into either **Jira tickets**
or a **requirement review**. A person writes `@Aetherion …` on a ticket; one reply comes
back under that comment.

| Agent | Registered as | Job | Writes to Jira? |
|---|---|---|---|
| **Jira Orchestration** (router) | `JiraOrchestration` | Reads the comment, decides what is being asked, calls one child, posts the one reply, sends the email | the reply comment, a label |
| **Work Breakdown** | `JiraTaskCreation` | Turns a requirement into an Epic, Stories and Sub-tasks | creates tickets |
| **Requirement Review** | `JiraRequirementReview` | Finds gaps, ambiguities and missing acceptance criteria | nothing |

---

## 1. How a comment travels (data in, reply out)

```
 Person comments on BGV-12:  "@Aetherion build"
        │
        ▼
 Jira webhook "Aetherion Jira Test" ─── signed with a secret ───►  Aetherion (wh.sbox.aetherion.io)
        │                                                          checks the signature (401 if wrong)
        ▼
 Event Subscription: "Issue Commented" + comment.body contains @Aetherion
        │
        ▼
 ROUTER (JiraOrchestration workflow)
   1. ingress_check   read the issue + comment from Jira
                      ignore silently: no @Aetherion · the agent's own reply · already answered ·
                      project not allowed (ORCH_ALLOWED_PROJECT_KEYS) · no issue · no comment
                      anything that FAILS (403, 500, network) → "Could not read this ticket" reply
   2. classify        Layer 1: the first word — build / review / explain / help
                      Layer 2: AI model decides (BUILD · REVIEW · QUESTION · CHATTER) with a confidence
                      Layer 3: unsure → asks "Which did you mean?"
   3. dispatch        starts ONE child workflow on that child's task queue
                      (ORCH_TASK_QUEUE_JIRA_TASK_CREATION / _REQUIREMENT_REVIEW)
        │
        ▼
 CHILD agent works (sections 2 and 3) and returns a RESULT CONTRACT
   { outcome, headline, summary, created[], duplicates[], questions[], findings[],
     sources_read[], sources_missing[], errors[] }
        │
        ▼
 ROUTER finishes
   4. compose         one reply built from the contract (same layout for every agent)
   5. email           only for: needs-info · already-exists · failed   (never for created / review)
   6. post_reply      posted IN THE THREAD of the asking comment; label + "answered" record stored
                      if posting fails → the administrators are emailed
```

**Rules that always hold:** one comment → at most one reply · the agent never answers its own
reply · a redelivered comment is not answered twice · a failure is never silent.

---

## 2. How Work Breakdown creates tickets

```
 READ everything        description · comments · attachments (.docx .pdf .txt …, incl. meeting
                        transcripts) · linked Confluence pages · linked issues · history
        │
 WHAT IS THE REQUEST?   the comment itself ("@Aetherion build let HR managers…"), or — if the
                        comment is only "build" / "build based on the description" — the ticket
        │
 TRIAGE (AI)            usable requirement? → no: "Invalid request" · too thin: asks specific
                        questions (email) · a question: "About BGV-12" answer, nothing created
        │
 BREAKDOWN (AI)         Epic (Medium/Large) → Stories → Sub-tasks, each Story with Given/When/Then
                        acceptance criteria and "Open questions — not stated in the requirement"
        │
 CHECKS (code + AI)     every listed line is covered by a Story or Sub-task (Epic doesn't count)
                        every HARD FACT of each line is in its Story: quoted names ("Price missing"),
                          numbers with units (30 days, 06:00), formats (INV-YYYYMM), limits
                          ("only one reminder"), "every …", "not twice", "recorded with …"
                        2+ Sub-tasks per Story · 2+ criteria · real completion checks
                        people as users (never "As a system") · no boilerplate
                        self-review: nothing stated was dropped, nothing stated is asked
                        "Out of scope" items are never demanded as work
        │
 ONE REGENERATION       anything wrong → the AI is asked once, naming exactly what is wrong
        │
 REPAIR                 missing facts → a small focused request; still missing → the requirement's
                        own sentence is added as a criterion. Vague completion criteria → rewritten.
        │
 DUPLICATES             compared with existing tickets (word match + AI "same work?" check)
                        all exists → "This work already exists" with keys · part exists → says how
                        to build only the missing part · identical earlier request → reused
        │
 CREATE IN JIRA         Epic → Stories (parent = Epic) → Sub-tasks (parent = Story)
                        works with "Sub-task" (company-managed) and "Subtask" (team-managed)
                        one piece of work → Sub-tasks under the asking ticket instead
                        the asking ticket gets a "relates to" link to the new Epic/Story
        │
 RESULT                 created keys + titles · "Details I could not determine — please confirm"
                        (or "Nothing was missing — every detail came from your description")
```

**No AI, no tickets:** if the AI model cannot produce the breakdown, nothing is created and the
reply says so — there is no fallback that writes generic tickets.

---

## 3. How Requirement Review works

```
 READ   the ticket · its parent · its Sub-tasks · linked issues · attachments · Confluence pages
 AI     findings in nine categories (assumption, open question, gap, ambiguity, edge case,
        missing functional / technical detail, missing acceptance criterion, dependency)
        + readiness level and score (e.g. "Needs minor clarification (4/5)")
 CHECKS every finding must cite a source that was read — otherwise dropped
        findings about technology no source mentions (API, SMTP, database…) — dropped
        open questions already on the ticket or its parent — not repeated
        a Sub-task is judged by its completion criteria and its Story's acceptance criteria
        dropped findings are counted as "over-reach" (the review's own quality number)
 RESULT readiness · findings · what was read · what was missing   — nothing is created
```

---

## 4. Where things are configured

| Setting | Where | Notes |
|---|---|---|
| Jira URL, account, API token | each agent's `.env` (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`) | the token acts as the person the agent posts as |
| Allowed projects | `ORCH_ALLOWED_PROJECT_KEYS`, `LTW_ALLOWED_PROJECT_KEYS` | currently `FL,BGV` |
| Child task queues | router `.env`: `ORCH_TASK_QUEUE_*` | `<agent id>-task-queue`, from the Temporal UI |
| Emails | `ORCH_NOTIFY_EMAILS`, `ORCH_NOTIFY_ON`, `GMAIL_*` | outcomes: clarification, duplicates, failed |
| AI models | `ORCH_CLASSIFIER_MODEL` (router), `LTW_LLM_MODEL` (breakdown) | via the Aetherion AI Gateway |
| Webhook signing secret | **Jira** webhook + **Aetherion → Setup → Apps → Jira** | must be identical; the agents never see it |

`.env` travels inside the published agent. `shared/` is mirrored into each agent with
`python3 scripts/sync_shared.py` — edit `shared/`, never the copies.

**Publishing:** from inside an agent folder, `uv run python scripts/publish.py --bump patch`
(tests → lint → version → `uv sync` → `aetherion publish`). Publish the **router first** when the
result contract changes. "Upload accepted" is not "live" — confirm the version in **Agent List**.

---

## 5. When nothing replies — check in this order

1. **Webhook secret** — Jira's webhook secret must equal Aetherion's Jira app secret. A mismatch
   rejects every event silently (`401 Invalid signature`). Re-check after editing the webhook.
2. **Event Subscription** — Active, filter only `comment.body contains @Aetherion`.
3. **Agent List** — the expected versions are live.
4. **Temporal** — a `JiraOrchestration` run at the time of the comment? Its status says where it
   stopped ("No workers polling" = the agent's worker is not running).
5. **The reply itself** — any failure after ingress is reported on the ticket, never silent.
