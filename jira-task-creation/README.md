# JiraTaskCreation

**Turns a Jira comment into a proper work breakdown.**

Someone comments this on a ticket:

> `@Aetherion Let depot staff record a tyre pressure check`

The agent reads the ticket, its attachments, its whole comment thread and any
linked Confluence pages, works out whether there is enough detail to build
something, and creates the Epic, Stories and Sub-tasks — then replies on the
ticket saying exactly what it did.

It behaves like an experienced person doing the breakdown by hand: it reads
everything first, invents nothing, and says so out loud when something could
not be read.

---

## Which page do I need?

| Page | Read it when you want to… |
|---|---|
| **README.md** (this page) | Install it, configure it, run it, deploy it |
| [BEHAVIOUR.md](BEHAVIOUR.md) | Know exactly what it replies and emails, word for word |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Understand the code — every folder, every function |
| [TESTING.md](TESTING.md) | Test it end to end on a real Jira project |
| [PUBLISH.md](PUBLISH.md) | Deploy it to the platform, safely |

**New to the project?** Read this page, then Part 1 and Part 2 of
[ARCHITECTURE.md](ARCHITECTURE.md). That is the fastest route to understanding
what this thing actually does.

---

## What it does, by example

| Someone comments | What the agent does |
|---|---|
| `@Aetherion Let drivers log a rest break of up to 30 minutes` | Creates a Story and Sub-tasks. Replies with links |
| *(the same comment again, a day later)* | **Creates nothing.** Replies pointing at the work that already exists |
| `@Aetherion Add export to the reports page` | **Creates nothing.** Asks 2–3 specific questions and emails them |
| `@Aetherion who is the PM?` | One short line back. No tickets, **no email** |
| `@Aetherion can you explain this ticket?` | Explains the ticket in plain English. Creates nothing |

Four guarantees:

- It **never creates duplicates**.
- It **never creates anything** from a comment that is not a requirement.
- It **always leaves exactly one reply** on the ticket, whatever happened.
- It **never replies to itself** — no loops.

---

## Before you start

You need:

| # | What | Notes |
|---|---|---|
| 1 | A **Jira Cloud site** | With a project to work in |
| 2 | A **Jira account for the agent** | Everything it creates shows this user as creator. A dedicated account is clearer than a personal one |
| 3 | A **Jira API token** | Create at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| 4 | **Python 3.12** and [uv](https://docs.astral.sh/uv/) | Python 3.13 will not work — the SDK is built for 3.12 |
| 5 | *(optional)* **Gmail app password** | Only if you want email alerts |

The Gmail password must be a **16-character app password** with 2-Step
Verification enabled. A normal account password will not work and Gmail will
reject it.

The agent's Jira account needs four permissions on the project:

| Permission | Needed for |
|---|---|
| Browse projects | Reading the ticket |
| Create issues | Creating the Epic, Stories, Sub-tasks |
| Edit issues | Linking Stories to the Epic, setting labels |
| Add comments | Writing the reply |

---

## Installing

```bash
git clone <this repo>
cd jira-task-creation
uv sync
cp .env.template .env
```

Open `.env` and fill in the four settings under **REQUIRED**:

| Setting | What to put |
|---|---|
| `JIRA_BASE_URL` | Your site address, no trailing slash — `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | The agent's Jira account |
| `JIRA_API_TOKEN` | The token from step 3 above |
| `LTW_WEBHOOK_SECRET` | Any long random string |

Generate the secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Then check your project is ready:

```bash
uv run python scripts/check_project.py FL
```

It reports every permission and issue type, and tells you which `.env` values
to set. **It changes nothing** — it only reads.

---

## Settings

All 49 live in `.env.template`, each with a two-line explanation. Below are the
ones that matter most, by group.

### Safety — set these before production

| Setting | Default | What it does |
|---|---|---|
| `LTW_ALLOWED_PROJECT_KEYS` | *(blank = any)* | Which projects it may write to. **Strongly recommended** |
| `LTW_READ_ONLY` | `false` | `true` refuses every write. A safe dry run — the breakdown is still produced and reported |
| `LTW_REQUIRE_LLM` | `false` | `true` refuses to run without the model rather than producing plainer wording. **Set this explicitly before production** — the preflight insists on it. With it off, a gateway outage still produces tickets, and although the reply comment and the email both say so in as many words, the tickets themselves are generically worded |

### Behaviour

| Setting | Default | What it does |
|---|---|---|
| `LTW_TRIGGER_KEYWORD` | `Aetherion` | The word that starts a run |
| `LTW_PROCESSED_LABEL` | `ltw-processed` | Label stamped once a request is answered |
| `LTW_AWAITING_LABEL` | `ltw-awaiting-input` | Label stamped when waiting on a person |
| `LTW_DUPLICATE_THRESHOLD` | `0.75` | How alike two **titles** must be to count as the same work |
| `LTW_DESCRIPTION_MATCH_THRESHOLD` | `0.85` | The same test against **descriptions** |
| `LTW_REQUIREMENT_MATCH_THRESHOLD` | `0.80` | How much of the **requirement** must already exist to count as a duplicate |
| `LTW_DUPLICATE_ADJUDICATE` | `true` | Ask the model about pairs that scored close but did not cross the line |
| `LTW_DUPLICATE_ADJUDICATE_FLOOR` | `0.30` | The bottom of that band. Below it, two titles share too little to be worth asking about |
| `LTW_MATCH_CLOSED_WORK` | `false` | `false` is correct — a decision not to build is not a duplicate |
| `LTW_CONTEXT_MAX_CHARS` | `24000` | Size budget. Least valuable sections drop first, and it says which |

### Attachments

| Setting | Default | What it does |
|---|---|---|
| `LTW_ATTACHMENT_MAX_FILES` | `10` | Files read per ticket |
| `LTW_ATTACHMENT_MAX_BYTES` | `10485760` | Largest single file (10 MB) |
| `LTW_ATTACHMENT_MAX_CHARS` | `20000` | Text kept per file |
| `LTW_ATTACHMENT_READ_IMAGES` | `true` | Reading images costs a model call each and slows the run |
| `LTW_READ_CONCURRENCY` | `4` | Attachments downloaded and read at the same time (at least 1) |
| `LTW_DUPLICATE_INDEX` | `true` | Compare against **every** ticket in the project (kept in memory; later builds read only what changed) |
| `LTW_INDEX_FULL_REFRESH_HOURS` | `24` | Full re-read this often; a match is checked to still exist before it is reported anyway |
| `LTW_PARALLEL_BREAKDOWN` | `true` | Plan first, then write the Stories at the same time (6+ requirement lines); same checks, one-call fallback |
| `LTW_LLM_CONCURRENCY` | `6` | Stories written at the same time |
| `LTW_WRITE_CONCURRENCY` | `4` | Tickets created in Jira at the same time |
| `LTW_LLM_FAST_MODEL` | empty | A quicker model for triage and the related-ticket check only; falls back to the main model |

**Supported file types:** PDF, Word (`.doc`, `.docx`), Excel (`.xls`, `.xlsx`),
PowerPoint (`.pptx`), `.csv`, `.json`, `.jsonl`, `.txt`, `.md`, and images
(`.png`, `.jpg`, `.webp`, `.gif`, `.bmp`, `.tiff`).

Anything else is reported as unsupported in the reply — never silently ignored.
A corrupt file is reported too, rather than passing a fragment off as content.

**Long files are not simply cut short.** The opening *and* the ending are both
kept, with `[... N characters omitted from the middle ...]` written in between.
Scope and acceptance criteria usually live at the end of a document, and plain
truncation threw exactly those away.

### Confluence

| Setting | Default | What it does |
|---|---|---|
| `LTW_CONFLUENCE_ENABLED` | `true` | Read linked wiki pages |
| `LTW_CONFLUENCE_MAX_PAGES` | `5` | Pages per ticket |
| `LTW_CONFLUENCE_MAX_CHARS` | `20000` | Text kept per page |
| `LTW_CONFLUENCE_READ_ATTACHMENTS` | `true` | Read files attached to those pages — the spec is often the attached sheet |

Only pages on the **same Atlassian site** are ever fetched. Anyone who can
comment on a ticket can put a URL in front of this code.

### Local PDF

| Setting | Default | What it does |
|---|---|---|
| `GENERATE_LOCAL_PDF` | `false` | `true`: `@Aetherion build` makes the whole breakdown into a PDF and **emails it to `LTW_NOTIFY_EMAILS`**. The reply on the ticket says where it went and lists what it proposes. **No Jira ticket is created, converted, labelled, edited or attached to.** `false`: the tickets are built. |

The PDF (`aetherion-breakdown-<KEY>-<hash>.pdf`) opens with an "At a glance" box
and a contents list, then: card numbers, what the ticket would become, sources
read and skipped, confirmed facts, missing information (including the
review's), the duplicate check, every proposed ticket numbered E / S1 / S1.1
with its full detail and criteria, risks, placement, and which ticket delivers
each requirement line — page numbers on every page. The same breakdown is not
emailed twice within the repeat window.

### Email

| Setting | Default | What it does |
|---|---|---|
| `GMAIL_SENDER` | | Leave blank to disable email entirely |
| `GMAIL_APP_PASSWORD` | | The 16-character app password |
| `LTW_NOTIFY_EMAILS` | | Who hears about requirements needing attention |
| `LTW_ADMIN_EMAILS` | *(falls back to above)* | Who hears about **system failures** |
| `LTW_NOTIFY_ON` | `clarification,duplicates,failed` | Which outcomes send email |
| `LTW_EMAIL_REPEAT_WINDOW` | `900` | Seconds an identical email is suppressed, so a retry cannot flood you |

`LTW_NOTIFY_ON` accepts any of `clarification`, `duplicates`, `failed`,
`created`. There is **no value that makes an invalid comment send email** —
that is deliberate and not configurable. See [BEHAVIOUR.md](BEHAVIOUR.md).

---

## Connecting Jira

Use **Automation for Jira**, not Jira's built-in System Webhooks — only
Automation can send the authentication header, and without it the request is
rejected.

**1. Create the rule.** Project settings → **Automation** → **Create rule**

**2. Trigger:** *Issue commented*

**3. Action:** *Send web request*

| Field | Value |
|---|---|
| URL | your endpoint, with `?tenant_id=<your tenant id>` |
| Method | `POST` |
| Headers | `X-Jira-Webhook-Secret` = your `LTW_WEBHOOK_SECRET` |
| Body | Custom data, including a top-level `event_type` of `issue.commented` |

**4. Add an Event Subscription** on the platform, with these filters:

| Field | Condition | Value |
|---|---|---|
| `comment.body` | contains | `@Aetherion` |
| `project` | equals | your project key |

**5. Under Advanced, set the Agent params template:**

```jinja
{"issue_key": "{{ structured_payload.issue_key }}",
 "comment_id": "{{ structured_payload['comment']['id'] }}"}
```

Without this the agent has no idea which ticket or comment it is answering, and
every run fails with *"Issue 'FL' was not found"*.

> **Do not add an extra filter to exclude the agent's own comments.** Filtering
> on `comment.body` contains `@Aetherion` is already enough: the agent's replies
> never contain a mention, so it cannot trigger itself. A second, fragile rule
> risks blocking genuine comments instead.

---

## Running it locally

Start the local server, which receives the webhook and runs the agent itself:

```bash
uv run python -m src.webhook.server
```

Expose it with ngrok and point your Automation rule at the public URL.

Run one ticket with no webhook at all:

```bash
uv run python scripts/run_issue.py FL-123
```

### The scripts

| Script | What it does | Writes anything? |
|---|---|---|
| `scripts/check_project.py FL` | Verifies a project is ready to use | No |
| `scripts/preflight_publish.py` | Shows what publishing would ship, and refuses obvious mistakes | No |
| `scripts/run_issue.py FL-123` | Runs the agent against one ticket | Yes |
| `scripts/canonical_probe.py` | Prints the event the platform would produce | No |
| `scripts/build_package.py --extract` | Builds the deploy package and shows its contents | No |
| `scripts/delete_issues.py` | Removes test tickets | Yes |

---

## Deploying

> **This deployment targets a test Jira instance on purpose.**
> `jiraagentdemo.atlassian.net` is a real Atlassian site created for testing, so
> the preflight's "looks like a test site" warning is accurate but expected.
> Publish with `--force-preflight` to accept it explicitly:
>
> ```bash
> uv run python scripts/publish.py --bump patch --force-preflight
> ```
>
> **Drop that flag** the moment `JIRA_BASE_URL` points at production — the
> warning should be taken seriously again at that point.


```bash
uv run aetherion publish
```

**Read [PUBLISH.md](PUBLISH.md) before you do.** It is a six-step checklist, and
two of the steps are not optional:

- **Bump the version in `pyproject.toml` first.** Publishing the same version
  over itself may leave the old build running — the upload succeeds and nothing
  changes.
- **`.env` ships inside the package.** Whatever is in it when you publish is
  what the deployed agent uses.

A successful upload does **not** mean the new build is live. PUBLISH.md explains
how to confirm it from the logs.

## Testing

```bash
uv run pytest -q                  # 878 tests
uv run ruff check src tests       # style
uv run black --check src tests    # formatting
```

For end-to-end testing on a real project, see [TESTING.md](TESTING.md).

### The shared package

Some of this agent's code is shared with the sibling review agent and lives in
`src/shared/`. **That directory is a mirror — never edit it.** The canonical
copy is `../shared/`, and `tests/test_shared_in_sync.py` fails if the two
diverge. After changing anything there:

```bash
python ../scripts/sync_shared.py     # re-mirror into every agent
uv run pytest -q                     # both agents consume it
```

What is shared, and why: attachment extraction, Atlassian Document Format,
Confluence reading, the environment readers, and the result contract the router
receives. See [../shared/README.md](../shared/README.md).

---

## When something looks wrong

| What you see | What it usually means |
|---|---|
| No reply at all | The Event Subscription is disabled, or its filters do not match |
| `Issue 'FL' was not found` | The Agent params template is not supplying the issue key |
| `RestrictedWorkflowAccessError` | A deployed build is older than the code. Bump the version and republish |
| Tickets created twice | Read the reply comment — it names exactly what it compared against |
| A thin breakdown | Read *"What I could not read"* in the reply. The spec was probably never opened |
| Generic, wooden ticket wording | Read *"⚠ Please review the wording"* at the top of the reply. The model was unreachable and the fallback wrote them |
| A duplicate still got created | The reply names what it compared against. Reworded duplicates are caught by the model; if it was unreachable, only word overlap ran |
| Repeated identical emails | A run is failing and retrying. `LTW_EMAIL_REPEAT_WINDOW` caps the noise; fix the run |
| Runs are slow | `LTW_ATTACHMENT_READ_IMAGES=true` costs a model call per image |

**The reply comment on the ticket is always the first place to look.** It says
what was read, what was not, and why.
