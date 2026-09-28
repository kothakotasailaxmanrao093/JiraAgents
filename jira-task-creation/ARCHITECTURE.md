# How it works, inside and out

This is the complete guide to the codebase. It assumes you have never seen this
project before and are not necessarily a Python developer.

Read it in order: the words first, then the journey one comment takes, then the
folder-by-folder reference.

---

## Part 1 — Words you need

| Word | What it means here |
|---|---|
| **Jira** | Atlassian's ticket tracker. The agent reads and writes tickets in it |
| **Issue / ticket** | One item of work in Jira, like `FL-123` |
| **Issue key** | The identifier, e.g. `FL-123` — project key `FL`, number `123` |
| **Epic / Story / Sub-task** | Jira's three levels of work. An Epic contains Stories; a Story contains Sub-tasks |
| **Comment** | A message someone posts on a ticket. This is how the agent is asked for things |
| **Webhook** | Jira telephoning us: "something happened." A message sent automatically |
| **Confluence** | Atlassian's wiki. Teams write the real specification there and link it from the ticket |
| **ADF** | *Atlassian Document Format.* Jira does not store plain text — it stores a nested JSON structure describing paragraphs, bullets and tables |
| **Attachment** | A file on a ticket: PDF, spreadsheet, screenshot |
| **Requirement** | What someone is asking to be built |
| **Breakdown** | Splitting one requirement into Epic / Stories / Sub-tasks |
| **LLM** | The language model that writes the wording |
| **Temporal** | The system that runs the agent reliably, retrying on failure |
| **Workflow** | The ordered list of steps. Runs under strict rules (see Part 5) |
| **Activity / tool** | One step. Unlike the workflow, it may talk to the network |
| **Idempotent** | Running it twice has the same effect as running it once — no duplicates |

---

## Part 2 — The journey of one comment

```
  Someone comments "@Aetherion Let drivers log a rest break" on FL-123
                          |
                          v
  [1] Jira sends a webhook ................ Automation for Jira
                          |
                          v
  [2] Is this really for us? .............. src/jira/trigger.py
      - mentions @Aetherion?                src/jira/keywords.py
      - not our own reply?
      - not already answered?
                          |
                          v
  [3] Read EVERYTHING ..................... src/context/ingest.py
      description, all comments,            src/context/attachments.py
      history, work log, attachments,       src/confluence/client.py
      linked issues, Confluence pages,      src/jira/trigger.py
      files attached to those pages
                          |
                          v
  [4] What kind of comment is this? ....... src/classification/request.py
                                            src/classification/validate.py
          +---------------+---------------+
          |               |               |
     not a req.       too thin          valid
          |               |               |
          v               v               v
     one line        2-3 questions   [5] Already exists?
     NO email        + email             src/jira/duplicates.py
                                              |
                                    +---------+---------+
                                    |                   |
                                 exists              new work
                                    |                   |
                                    v                   v
                              links + email     [6] Break it down
                                                  src/classification/decompose.py
                                                        |
                                                        v
                                                [7] Create in Jira
                                                  src/jira/issues.py
                                                        |
                          +-----------------------------+
                          v
  [8] Reply on the ticket ................. src/jira/trigger.py
      ALWAYS, in every outcome
                          |
                          v
  [9] Email, or deliberately not .......... src/notifications/email.py
```

### Step 1 — The webhook arrives

An **Automation for Jira** rule fires when a comment is added and sends it to
Aetherion. Automation is used rather than Jira's built-in System Webhooks
because only Automation can send the authentication header, and without a
header the request is rejected with a 401.

The event reaches `/webhooks/v2/jira?tenant_id=...`, is converted to a standard
shape, and starts an agent run. An **Event Subscription** decides which events
reach this agent. Its **Agent params template** picks out the two facts the
agent cannot work without:

```jinja
{"issue_key": "{{ structured_payload.issue_key }}",
 "comment_id": "{{ structured_payload['comment']['id'] }}"}
```

Without this the agent has no idea which ticket or comment it is answering.

Not every event carries an issue. Those render to the bare project key (`FL`)
and are dropped immediately — no run, no email. The log says:

```
'FL' is not an issue key; the event carried no issue. Nothing was done.
```

### Step 2 — Is this for us?

Three checks, all of which must pass:

1. **Does it mention the keyword?** `@Aetherion` by default.
2. **Is it one of our own replies?** Every reply ends `— AetherionAgent ·
   automated reply` and contains no mention, so the agent cannot answer itself
   and spiral into a loop.
3. **Has that comment already been answered?** Tracked per comment id, stored
   on the Jira issue itself so it survives restarts.

The third matters more than it looks. Jira delivers webhooks more than once,
and people click twice. Without it, every later delivery re-runs the same work.

### Step 3 — Reading everything

This is the step that decides whether the output is good. Before deciding
anything at all, the agent gathers:

| Source | Where from |
|---|---|
| Summary and description | `trigger.fetch_issue` |
| The `@Aetherion` comment | `trigger.parse_comments` |
| **Every** comment | `trigger.parse_comments` — key detail is often in an earlier one |
| Change history | `trigger.parse_history` |
| Work log | `trigger.parse_worklogs` |
| Attachments | `context/attachments.py` |
| Linked work items | `trigger.parse_links` |
| Linked Confluence pages | `confluence/client.py` |
| Files attached to those pages | `confluence.fetch_page_attachments` |

Everything is assembled into one text by `ingest.assemble_requirement`.

**Nothing is skipped silently.** A file that could not be parsed, a page behind
permissions, a section dropped to fit the budget — each is reported in the
reply comment under *"What I could not read"*. A thin breakdown must never be
mistaken for a bad requirement when the truth is the spec was never opened.

**Two size limits, both honest about what they did:**

- *Per file* (`LTW_ATTACHMENT_MAX_CHARS`, 20,000). `fit_to_budget` keeps the
  opening **and** the ending, splitting on blank lines, and writes
  `[... N characters omitted from the middle ...]` into the text. Plain
  truncation used to keep only the opening, which is the worst half to keep —
  scope, limits and acceptance criteria are almost always at the end.
- *Overall* (`LTW_CONTEXT_MAX_CHARS`, 24,000). Whole sections are dropped,
  least valuable first (work log, then history, then comments…), never cut
  mid-sentence, and each drop is named in the reply.

**Security note.** Only Confluence URLs on the *same* Atlassian site are
fetched. Anyone who can comment on a ticket can put a URL in front of this
code; following arbitrary addresses would turn the agent into a proxy for
whatever network it runs on.

### Step 4 — What kind of comment is this?

`classification/request.py` sorts every comment into one of four buckets:

| Bucket | Meaning | Example |
|---|---|---|
| **NOT_A_REQUIREMENT** | No work is being asked for | "who is the PM?", "thanks" |
| **INCOMPLETE** | Real work, not enough detail | "Add export to the reports page" |
| **VALID** | Enough to work from | "Let drivers log a break of up to 30 minutes" |
| **UNCERTAIN** | Readable and substantial, but no verb the list knows | "Encrypt the licence numbers at rest" |

**UNCERTAIN** exists because `_ACTION_WORDS` is hand-maintained and has been
wrong in production three times, each time rejecting a real requirement. Rather
than reject on a list miss, the text goes to the model, which resolves it to
one of the other three. If the model is unreachable it becomes **INCOMPLETE** —
asked about, never rejected. Chatter does not reach it: a question about the
world or somebody talking about themselves is filtered out first, so "who is
the PM of India?" still lands in **NOT_A_REQUIREMENT** without a model call.

The split between the first two is the one that matters. The first asks *is
there work here at all*; the second asks *is there enough detail*. Getting them
backwards means replying "which project is this for?" to someone saying hello.

A comment is **VALID** when any of these hold: it asks for more than one thing;
it is twelve words or longer; it names a role *and* a specific detail; or it
names a role and is at least six words. Otherwise it is **INCOMPLETE** and gets
questions. When in doubt the code asks rather than builds — an unnecessary
question costs one comment, a wrong Epic costs a clean-up.

### Step 5 — Does it already exist?

Three independent checks, in `jira/duplicates.py`:

| Check | Compares | Threshold | Catches |
|---|---|---|---|
| **Idempotency label** | An exact fingerprint of requirement + project + comment | exact | The same comment processed twice |
| **Title similarity** | Proposed titles vs existing summaries | 0.75 | The same work, similarly worded |
| **Requirement text** | What the person typed, vs existing summaries *and* descriptions | 0.80 | The same work worded **differently** |

The third is the important one, and it exists because the first two failed in
production. Two runs of one requirement produced *"Allow depot staff to record
tyre pressure checks"* and *"Staff record a tyre pressure check"* — different
generators, different vocabulary — and neither title check crossed its
threshold. A duplicate Story was created.

The requirement text is byte-identical on every re-post, so it is the only
dependable comparison. Measured on the real project: the genuine duplicate
scored **0.833**, the nearest unrelated ticket **0.333**.

Closed work is ignored by default (`LTW_MATCH_CLOSED_WORK=false`). Work closed
as "Won't Do" is a decision *not* to build something; that is not a reason to
refuse a new request.

### Step 6 — Breaking it down

Size is decided first, from the number of distinct capabilities asked for:

| Size | Shape |
|---|---|
| Small | One Story with Sub-tasks — no Epic |
| Medium | One Epic, Stories, Sub-tasks |
| Large | One Epic, more Stories, Sub-tasks |

Wording comes from the language model where available. When the gateway is
unreachable, `heuristic_breakdown` produces the same structure with plainer
wording, and the reply says so — the tickets still appear, just thinner.

Whatever wrote them, **every item is validated before anything reaches Jira**:
required fields present, the user story in *"As a … I want to … so that …"*
form, and no invented source filenames.

### Step 7 — Creating in Jira

Checked **before the first write**: the project exists, the issue types exist,
the account has permission. A missing Sub-task type fails with nothing created
rather than half a hierarchy.

Everything created carries a fingerprint label (`ltw-…`) so a repeat delivery
finds the existing work instead of making it again. Success is logged:

```
Created in FL: 4 issue(s) [epic=- | stories=['FL-125'] | subtasks=[...]] label=ltw-...
```

### Steps 8 and 9 — The reply and the email

Always exactly one reply comment, in every outcome. Email only when a person
has something to do. Both are documented in full in
[BEHAVIOUR.md](BEHAVIOUR.md).

---

## Part 3 — The codebase, folder by folder

```
src/
  agent/          the run itself — which step, in which order
  tools/          the steps, each able to reach Jira and the network
  context/        reading a ticket and its attachments
  confluence/     reading linked wiki pages and their attachments
  classification/ deciding kind, size, and the breakdown
  jira/           everything that talks to Jira
  notifications/  email
  webhook/        a local server, for running without the platform
  models/         the shapes everything must fit
  config/         reading settings from the environment
  prompts/        what the language model is told
```

### `src/agent/` — the order of events

**`agent.py`** is the workflow. It contains no cleverness: it calls each step in
turn and decides where to stop. Every exit path either posts a reply or has a
stated reason not to.

It may **not** read settings — see Part 5.

### `src/tools/` — the steps

**`tools.py`** holds eight activities. These may reach the network.

| Tool | What it does |
|---|---|
| `read_jira_issue` | Reads the ticket and everything attached to it. The biggest step |
| `inspect_jira_context` | Reads the project, its existing tickets, the epic, the sprint |
| `validate_requirement` | Decides the bucket: not-a-requirement / incomplete / valid |
| `generate_work_breakdown` | Produces the Epic/Stories/Sub-tasks, and checks for duplicates |
| `create_jira_issues` | Writes the hierarchy to Jira |
| `report_to_issue` | Posts the reply comment and sets the marker labels |
| `notify_email` | Sends the email, or decides not to |
| `jira_configuration_status` | Reports whether Jira is correctly configured |

### `src/context/` — reading the ticket

**`ingest.py`** turns raw ticket data into one usable requirement.

| Function | What it does |
|---|---|
| `assemble_requirement` | Builds the final text, plus notes on anything dropped |
| `build_sections` | One labelled section per source |
| `clean_requirement_text` | Everything that turns raw text into a usable requirement |
| `strip_trigger_mentions` | Removes `@Aetherion`, keeps the requirement around it |
| `strip_agent_instructions` | Removes sentences addressed to the agent, not stating work |
| `requirement_core` | Keeps only what states a requirement |
| `detect_placement` | Reads "add these to the current sprint" out of the request |
| `is_question_comment` | True when someone is asking to be *told* something |
| `is_pointer_comment` | True when someone says "work from this ticket" |
| `describe_issue` | Plain-English answer to "what is this ticket?" |
| `find_scope_exclusions` | Things a later comment said *not* to build |
| `find_conflicts` | Where a linked page contradicts the ticket |
| `find_unread_links` | Links that contributed nothing |
| `request_is_only_a_pointer` | True when the request is nothing but a link |

**`attachments.py`** extracts text from files.

| Function | What it does |
|---|---|
| `extract` | Reads one file. **Never raises** — a bad file is reported, not fatal |
| `supported` | Whether this file type can be read |
| `fit_to_budget` | Shortens long text, keeping the opening **and** the ending |

Supported: PDF, `.doc`, `.docx`, `.xls`, `.xlsx`, `.pptx`, `.csv`, `.json`,
`.jsonl`, `.txt`, `.md`, and images. Anything else is reported as unsupported.

### `src/confluence/` — reading linked pages

| Function | What it does |
|---|---|
| `fetch_linked_pages` | Reads every page the ticket points at |
| `fetch_page` | Reads one page |
| `fetch_page_attachments` | Reads files hanging off that page |
| `find_references` | Finds Confluence URLs in text |
| `parse_reference` | Understands all four URL shapes Confluence produces |
| `storage_to_text` | Flattens Confluence's HTML into readable text |

### `src/classification/` — the thinking

**`request.py`** — `classify_request` returns the bucket and any questions.

**`validate.py`**

| Function | What it does |
|---|---|
| `validate_input` | The deterministic gate over an incoming requirement |
| `has_action_intent` | True when the text names something to be done |
| `starts_with_verb` | True when the first word is an action verb |

**`decompose.py`** — the largest file, and the one that writes the tickets.

| Function | What it does |
|---|---|
| `build_breakdown` | The entry point. Prefers the model, falls back cleanly |
| `heuristic_breakdown` | A valid breakdown with no model at all |
| `llm_breakdown` | Asks the model, validates the answer against the schema |
| `triage` / `llm_triage` | Second opinion on scope and missing information |
| `split_capabilities` | Splits a requirement into distinct things being asked for |
| `classify` | Small / Medium / Large, from that count |
| `extract_actor` | Splits "allow tenants to X" into `("tenants", "X")` |
| `to_infinitive` | Makes the action read correctly after "I want to …" |
| `drop_excluded` | Removes capabilities a later comment ruled out |

### `src/jira/` — everything that talks to Jira

Split by job so no single file becomes unreadable.

| File | Responsibility |
|---|---|
| `client.py` | Credentials, the HTTP client, issue-type names, the read-only switch |
| `keywords.py` | The trigger word, the marker labels, recognising our own comments |
| `adf.py` | Reading and writing Atlassian Document Format |
| `trigger.py` | Reading an issue, its comments, history, links; posting the reply |
| `context.py` | The project, its existing issues, the epic, the board, the sprint |
| `duplicates.py` | All duplicate detection |
| `issues.py` | Creating the hierarchy |
| `api.py` | One import point for everything above |

Notable functions:

| Function | What it does |
|---|---|
| `fetch_issue` | Reads one issue with everything needed, in a single call |
| `parse_comments` / `parse_history` / `parse_worklogs` / `parse_links` | Flatten Jira's nested JSON |
| `outcome_comment` | Builds the reply comment |
| `add_comment` / `set_labels` | Write back |
| `mark_comment_answered` / `answered_comment_ids` | Per-comment memory |
| `create_hierarchy` | Creates the Epic/Stories/Sub-tasks |
| `idempotency_key` / `find_existing` | The fingerprint that prevents duplicates |
| `build_overlap_report` / `find_requirement_match` | Duplicate detection |
| `active_sprint` / `add_to_sprint` | Sprint placement |

### `src/notifications/` — email

| Function | What it does |
|---|---|
| `send` | Sends one email. **Never raises** — a mail failure never loses tickets |
| `should_notify` | Whether this outcome is worth an email at all |
| `recipients_for` | Failures go to the admin list; everything else to the team |
| `_subject_for` | Builds a subject that states the outcome on its own |
| `build_body` | What happened, why, what is needed, links |

### `src/models/` — the shapes

**`schemas.py`** defines every structure that crosses a boundary. All reject
unknown fields, so the language model cannot smuggle in extras.

| Shape | What it holds |
|---|---|
| `Subtask` / `Story` / `Epic` | The contracted fields, and only those |
| `WorkBreakdown` | One complete breakdown |
| `SourceIssue` | Everything read from the trigger ticket |
| `JiraContext` | The project, existing issues, epic, sprint |
| `DuplicateMatch` / `OverlapReport` | What overlapped what |
| `JiraResult` | What was created |
| `TicketWiseResult` | The complete outcome of a run |
| `AttachmentText` / `ConfluencePage` | One file, one page — with a note when unreadable |

Two rules are enforced here, not by convention:

- A user story must read *"As a … I want to … so that …"*. Checking the three
  keywords was not enough — it let "I want send rent reminder emails" through —
  so the word after "I want" is checked too.
- Sub-tasks may not name source files the person did not mention. Echoing back
  a filename they wrote is fine; inventing one is not.

---

## Part 4 — Configuration

Every setting lives in `.env`, with a two-line explanation, and is read through
`src/config/settings.py` (`env_bool`, `env_int`, `env_float`, `env_list`).

Grouped as: **REQUIRED**, **SAFETY**, **JIRA**, **BEHAVIOUR**, **CONFLUENCE**,
**ATTACHMENTS**, **EMAIL**, **AI MODEL**, **SERVICE**. See
[README.md](README.md) for the ones you are most likely to change.

---

## Part 5 — Two constraints that will bite you

### The workflow may not read settings

`agent.py` runs inside a **Temporal workflow**, which must behave identically
every time it replays. Reading the environment, the clock, or a random number
would break that, so Temporal blocks it outright:

```
RestrictedWorkflowAccessError: Cannot access os.environ.get from inside a workflow
```

This is not theoretical — it took the agent down in production. A single call
to a helper that read one setting crashed every run.

**The rule:** every decision that depends on configuration is made inside a
*tool* and passed to the workflow as a plain value. `tools.py` computes
`trigger_is_question`; `agent.py` only reads the answer.

`tests/test_workflow_sandbox.py` enforces this. If `agent.py` ever imports
something that can reach the environment, the build fails.

### This code can exist twice in one process

The project is importable both from the source tree and from the installed
package. Python then builds **two module objects with two separate sets of
globals**. A value stored at module level is written by one copy and read by
the other.

This was found while debugging the email repeat-suppression ledger, which
silently suppressed nothing because of it. The ledger now lives on `sys`, the
one object guaranteed to be shared.

**The rule:** anything that must be shared across the whole process cannot live
in module scope.

---

## Part 5b — Delegated mode, and who owns the reply

*(This file is this agent's WORKING.md — how it works inside. It predates that
name; there is one document, not two.)*

### Three ways in, one implementation

| Mode | Triggered by | Creates issues | Posts a reply | Labels | Emails | Returns |
|---|---|---|---|---|---|---|
| Webhook | Jira Automation | yes | yes | yes | yes | `TicketWiseResult` |
| Manual | UI / CLI / API | yes | no (no issue) | no | yes | `TicketWiseResult` |
| **Delegated** | **Jira Orchestration** | **yes** | **no** | **no** | **no** | **`AgentResult`** |

The mode is decided once, in the `@agent()` wrapper, and the delegated result is
translated once, in the same place. The breakdown itself does not branch on it.

### Why it is a ContextVar and not a parameter

There are nine write-back sites and eight notify sites across twelve exits.
Threading a flag to each would mean editing every outcome branch to add a mode —
precisely what Open/Closed says to avoid — and a branch missed in that edit would
post a second comment in production **without failing a single test**.

So the mode gates the two write functions instead: `_report` and `_notify`. One
place per side effect where the decision is made.

It is set on *every* run and reset in a `finally`, not set only when delegated.
Setting it only on the delegated path left it true for whatever ran next in the
same context, silently switching write-back off for a direct run. That leak is
invisible — nothing errors, the reply simply never appears — and it was caught
by `test_delegated_mode_does_not_leak_into_the_next_run`.

### Why the router posts the reply at all

The obvious alternative is for each agent to post and sign its own reply. It was
rejected, and the reasoning belongs in the record:

- exactly-once stops being structural and becomes a promise three services each
  have to keep;
- a route that runs both children produces two comments the user must reconcile;
- formatting drifts as each agent grows its own comment builder;
- a half-failed child leaves either no comment or a misleading one;
- a fourth agent means a fourth comment writer to keep in step.

**Attribution is a content concern, not a transport concern.** The byline costs
one line of text inside the comment. It is not worth three writers.

### Guarantees that move, and how each survives

| Guarantee | Delegated owner | How it survives the handover |
|---|---|---|
| Duplicate protection | **this agent** | unchanged — it runs before any write, and the router never creates anything |
| Idempotency label on the created hierarchy | **this agent** | unchanged — applied by `create_jira_issues`, not by the reply path |
| Answered-comment property | **the router** | only the router knows whether the single reply actually posted; recording it here would mark a comment answered even when the reply failed |
| Marker label on the issue | **the router** | same reason — the label means "this was answered", which is the router's fact |
| Never email an invalid request | **the router** | the child only ever *recommends*. `_EMAIL_KIND` has no entry for `invalid_request`, so a recommendation is not even expressible; and `NOT_A_REQUIREMENT` refuses one regardless |
| The four outcomes | **this agent** | mapped to the contract's closed set by `_OUTCOME_MAP`; an unknown status fails safe to `FAILED` rather than reaching the router unmapped |
| "Please review the wording" on a heuristic run | **both** | the child sets `degraded`, the router renders it |

### What the router receives

`src/agent/delegated.py::to_contract` translates the completed run into
`shared.contract.AgentResult`. It is a pure function over a result that has
already happened, so it cannot change what the agent did — only how it is
described.

`sources_read` is built from what the read step actually returned, so it can
never claim a source that failed; anything unreadable appears in
`sources_missing` instead, with what to do about it.

---

## Part 6 — Where to change things

| You want to… | Go to |
|---|---|
| Change what the reply says | `src/jira/trigger.py` → `outcome_comment` |
| Change an email subject or body | `src/notifications/email.py` → `_subject_for`, `build_body` |
| Change when email is sent | `src/notifications/email.py` → `should_notify` |
| Support a new file type | `src/context/attachments.py` → `_extract_sync`, `supported` |
| Change duplicate sensitivity | `.env` thresholds, or `src/jira/duplicates.py` |
| Change Small/Medium/Large | `src/classification/decompose.py` → `classify` |
| Change what the model is told | `src/prompts/templates.py` |
| Change the steps or their order | `src/agent/agent.py` |
| Add a new step | `src/tools/tools.py`, then call it from `agent.py` |

**Before changing anything**, run `uv run pytest -q`. 853 tests exist, and most
were written because something broke in production. If one fails after your
change, it is telling you about a real case.

---

## Part 7 — Running it without the platform

`src/webhook/server.py` does the same job locally: receives the webhook,
verifies the shared secret, runs the agent. Useful for development and for
checking a change before publishing.

```bash
uv run python -m src.webhook.server
```

See [TESTING.md](TESTING.md) for the full local setup, and
[PUBLISH.md](PUBLISH.md) for deploying to the platform.
