# Testing Aetherion, from scratch

A runbook. Start with an empty Jira project and work down. Each part builds on
the one before — the duplicate tests only mean something once earlier parts have
put real tickets in the project.

Every **Expect** block below is the agent's actual output, not an approximation.

---

## Contents

- [Part 0 — Set everything up](#part-0--set-everything-up)
- [Part 1 — The first task](#part-1--the-first-task)
- [Part 2 — Build the project up](#part-2--build-the-project-up)
- [Part 3 — Never create it twice](#part-3--never-create-it-twice)
- [Part 4 — Reading the whole ticket](#part-4--reading-the-whole-ticket)
- [Part 5 — Edge cases](#part-5--edge-cases)
  - Attachments, Confluence, duplicates, email discipline
- [Results sheet](#results-sheet)

---

## Part 0 — Set everything up

### 0.1 The Jira project

This runbook uses the project key **`FL`**.

**If you already have `FL`** — confirm it is empty before starting:

```bash
python scripts/check_project.py FL
```

Look for `existing tickets  0 read`. If it is not zero, delete the leftovers
first. Part 3 tests that work is never created twice, and old tickets will make
those results meaningless.

> **Delete tickets, do not close them.** Closed work is deliberately ignored by
> duplicate detection, so closing leftovers will not clear the way.

**If you are starting a new project** — Jira → **Projects** → **Create
project** → *Scrum* or *Kanban* (either works). Name it something like
`Fleet Link`, and set the key to `FL`.

The key is what the agent works with. It reads and writes in exactly one
project: the one containing the ticket you comment on.

### 0.2 Point the agent at it

In `.env`:

```bash
JIRA_PROJECT_KEY=FL
LTW_ALLOWED_PROJECT_KEYS=FL
```

`LTW_ALLOWED_PROJECT_KEYS` is a safety fence — the agent will refuse to write
anywhere else, even if someone comments on a ticket in another project.

### 0.3 Check the project is ready

```bash
python scripts/check_project.py FL
```

This only reads. Expect:

```
[OK  ] perm create_issues          create the Epic, Stories and Sub-tasks
[OK  ] perm edit_issues            link stories to the epic, set marker labels
[OK  ] perm add_comments           write the outcome comment
[OK  ] issue types                 Epic, Story, Task, Sub-task, Bug
[OK  ] existing tickets            0 read (used for duplicate detection)
[OK  ] trigger keyword             Aetherion

READY. Project 'FL' can be used with the agent.
```

**`existing tickets 0 read` matters.** Part 3 depends on starting empty.

If anything says `[FAIL]`, fix that before going further — the message tells you
what to change.

### 0.4 Start the service

**Restart it even if it is already running.** A running receiver holds the code
it started with; anything changed since is not live.

```bash
# stop any old one
pkill -f "uvicorn src.webhook.server"

# start fresh
python -m uvicorn src.webhook.server:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
ngrok http 8000
```

Copy the `https://` address it prints, for example
`https://chapped-grievance-blinks.ngrok-free.dev`.

Confirm it is alive:

```bash
curl -s localhost:8000/health | python -m json.tool
```

`secret_required` must be `true`. If it is false, no secret is configured and
**every delivery will be refused with 503** — that is deliberate, because this
endpoint creates Jira tickets.

### 0.5 Connect the webhook

Jira → **Settings** → **System** → **WebHooks** → **Create a WebHook**

| Field | Value |
| --- | --- |
| **URL** | `https://your-ngrok-address/jira/webhook` |
| **Events** | ☑ Comment: created |
| **Secret** | The same value as `LTW_JIRA_WEBHOOK_SECRET` in your `.env` |

> **The URL must end in `/jira/webhook`.** Pointing it at the bare address is the
> most common mistake — Jira gets a 405 and nothing happens.

### 0.6 Create the one starting ticket

Create a single ticket in `FL`:

| Field | Value |
| --- | --- |
| Type | Task |
| Summary | `Fleet requests` |
| Description | *(leave empty)* |

This is your working ticket. You will comment on it repeatedly.

> **Your ticket will not be `FL-1`.** Jira never reuses issue numbers, so if this
> project has held tickets before, the new one continues from where it left off —
> `FL-71`, say. The examples below write `FL-1`, `FL-2`; substitute whatever keys
> Jira actually gives you. Only the *shape* of each result matters.

---

## Part 1 — The first task

### T1 · One capability → no Epic

**Comment on your working ticket:**

```
@Aetherion Allow drivers to upload a photo of a delivered parcel.
```

**Expect** — `Small`. **No Epic.** One Story, three Sub-tasks under it.

```
AetherionAgent · Work breakdown created.

What happened
  1 Story, and 3 Subtasks created successfully.

Created in Jira
  Story    FL-2 — Upload a photo of a delivered parcel
  Sub-task FL-3 — Define the rules for: Allow drivers to upload a photo…
  Sub-task FL-4 — Implement the behaviour for: Allow drivers to upload…
  Sub-task FL-5 — Verify and hand over: Allow drivers to upload a photo…

— AetherionAgent · automated reply
```

Check in Jira that all three Sub-tasks sit **under the Story**, not loose in the
backlog.

**Fails if:** an Epic appears, or more than one Story, or the Sub-tasks have no
parent.

> **Why a Story, rather than Sub-tasks on your own ticket?** Because your comment
> carried the requirement and the ticket did not — `Fleet requests` says nothing
> about photographing parcels. Something has to describe the work, so a Story
> does.
>
> The other way round happens in **T10**: there the requirement is in the
> ticket's description and the comment only points at it, so the ticket *is* the
> work item and the Sub-tasks attach to it directly. No duplicate Story gets
> created to repeat what the ticket already says.

**If nothing happens at all**, stop and check these in order: the webhook URL
ends in `/jira/webhook`; `/health` says `secret_required: true`; the receiver
terminal logs a line for every delivery it declines, with the reason.

---

## Part 2 — Build the project up

**How to run each test in this part**, in two steps:

1. **Create a ticket** — type *Task*, **Summary** as given below,
   **Description left empty**.
2. **Post the comment** on that ticket.

The comment is the request; the ticket is only its container. The summary is
just a label so you can find it again — calling it `Test 2` would give an
identical result. You could even reuse `FL-1` for everything; separate tickets
simply keep fifteen requests and fifteen replies readable.

**Part 4 is the opposite**, and that is the point of it: there the requirement
goes in the ticket's *description*, and the comment only points at it.

### T2 · Two capabilities → Epic + 2 Stories

**Ticket:** `Servicing and depot alerts` · **Comment:**

```
@Aetherion Let managers schedule a vehicle service, and notify the depot when a vehicle is off the road.
```

**Expect** — `Medium`. An Epic, exactly **2** Stories, **6** Sub-tasks.

```
EPIC: Let managers schedule a vehicle service, and notify the depot when a
      vehicle is off the road
  STORY: Schedule a vehicle service
     As a manager, I want to schedule a vehicle service, so that I can do
     this myself instead of asking someone else.
  STORY: Notify the depot when a vehicle is off the road
     As a manager, I want to notify the depot when a vehicle is off the road,
     so that the right person hears about it without being chased.
```

**Fails if:** the two clauses merge into one Story, or split into three.

### T3 · Six capabilities → Epic + 6 Stories

**Ticket:** `Fleet tracking` · **Comment:**

```
@Aetherion Track servicing, tyre replacement, insurance renewal, fuel spend, licence expiry, and accident reports.
```

**Expect** — `Large`. An Epic, exactly **6** Stories, **18** Sub-tasks:
servicing / tyre replacement / insurance renewal / fuel spend / licence expiry /
accident reports.

**Fails if:** you get 5. This exact list caught a real bug once, where a prefix
match folded "insurance renewal" into the clause before it.

### T4 · Bullet list → one Story per bullet

**Ticket:** `Depot reporting`

Type this using Jira's **bullet-list button**, not typed dashes. That is what a
real user does, and it is what broke this test the first time.

```
@Aetherion Please add:
 • driver shift roster
 • fuel spend by depot
 • tyre replacement log
```

**Expect** — `Medium`, an Epic, exactly **3** Stories:

```
EPIC: Add: driver shift roster, fuel spend by depot and tyre replacement log
  STORY: Driver shift roster
  STORY: Fuel spend by depot
  STORY: Tyre replacement log
```

**Fails if:** you get one Story called
`Add: driver shift roster fuel spend by depot tyre replacement log`. A bullet
list typed in Jira carries no `-` characters of its own — if that structure is
lost, three capabilities read as one sentence. Also check no `-` survives into
the Epic summary.

### T5 · A list of values is not a list of work

**Ticket:** `Approval workflow` · **Comment:**

```
@Aetherion A request moves through four states: Draft, Submitted, Approved, and Rejected.
```

**Expect** — **one** Story, no Epic. The four states describe one workflow.

**Fails if:** you get four Stories named after statuses.

> The Story title here will read awkwardly — `A request moves through four
> states: Draft, Submitted…`. That is the known limitation below: the fallback
> restates your words rather than rephrasing them. The *count* is what this test
> checks.

---

## Part 3 — Never create it twice

These need Part 2 to have run. **Delete test tickets rather than closing them** —
closed tickets are correctly ignored, so closing will not give you the result
you expect.

### T6 · Exact duplicate → creates nothing

**Ticket:** `Servicing again` · **Comment:**

```
@Aetherion Let managers schedule a vehicle service.
```

**Expect** — **nothing created.**

```
AetherionAgent · Nothing created — this work already exists.

This requirement already exists in FL: "Schedule a vehicle service" is
covered by FL-nn ("Schedule a vehicle service", To Do).

Answers needed
  Is this genuinely new work? If so, reword the description so it is
  distinct from the tickets listed above and trigger the agent again.
  Otherwise, should the existing tickets be updated instead?
```

The ticket gets the `ltw-awaiting-input` label.

### T7 · Partly new → still creates nothing, but asks

**Ticket:** `Servicing plus reporting` · **Comment:**

```
@Aetherion Let managers schedule a vehicle service, and send a weekly fuel report to finance.
```

**Expect** — **nothing created.** The reply names the half that exists, names the
half that does not, and asks which you want.

**Fails if:** it creates the new half without asking. Partial overlap is a
question, not permission to proceed.

### T8 · Asking the same thing twice

Go back to **T1's ticket** and post the same comment again, as a **new** comment.

**Expect** — nothing new created:

```
Nothing was added. FL-1 already carries all 3 of these sub-tasks.
Delete them first if you want the work broken down again.
```

**Fails if:** you end up with six Sub-tasks. This is the case the idempotency
label alone does not catch, because a second comment produces a different label.

### T9 · Closed work does not block

Close one of T2's Stories as **Won't Do**, then ask for that work again on a
fresh ticket.

**Expect** — it **is** created. A decision not to build something is not a
reason to refuse building it later.

---

## Part 4 — Reading the whole ticket

### T10 · Uses the ticket's own description

**Ticket:** `Fault reporting`, **with** description
`Allow drivers to report a vehicle fault from their phone.`

**Comment:**

```
@Aetherion please break this down into stories
```

**Expect** — the comment is an *instruction*, not a requirement, so the
**description** is decomposed. Sub-tasks attach to the ticket; no new Story;
your description is not edited.

**Fails if:** it tries to build something called "break this down into stories".

### T11 · A question gets an answer, not tickets

On the same ticket:

```
@Aetherion please explain this ticket
```

**Expect** — **nothing created**:

```
AetherionAgent · About FL-nn

FL-nn is a Task, currently To Do, raised by <you>.
Its title is "Fault reporting".
What it asks for, in its own words:
Allow drivers to report a vehicle fault from their phone.

Nothing was created — this was a question, not a request for work. To have
it broken down, comment asking for that, for example "break this down into
stories".
```

Also try `what does this ticket mean?` and `summarise this ticket`.

**Fails if:** any ticket is created.

### T12 · A linked Confluence page

Create a Confluence page in any space, titled `Driver Rest Breaks`:

```
- Allow drivers to log a rest break.
- Let dispatchers see who is on a break.
- Send a weekly compliance report to the fleet manager.
```

**Ticket:** `Rest breaks` · **Comment:** paste the page URL and nothing else:

```
@Aetherion https://yoursite.atlassian.net/wiki/spaces/.../Driver+Rest+Breaks
```

**Expect** — the page becomes the requirement: an Epic and **3** Stories, one per
bullet.

**Fails if:** the URL appears in any ticket title, or the Epic summary contains
`(no description provided)`.

> Jira may turn your pasted URL into a **smart link** showing only the page
> title. That still works — the agent reads the underlying address.

### T13 · A page it cannot read

Take the T12 URL and change the page id to one that does not exist.

```
@Aetherion Build what this spec says: https://YOURSITE.atlassian.net/wiki/spaces/YOURSPACE/pages/999999/Gone
```

> **Use your own site name**, not the placeholder. A different `*.atlassian.net`
> host is refused as foreign and never fetched, so you would get **silence**
> instead of the message below — that is E8, a different test. The URL must also
> contain `/wiki/`: a Jira `/browse/FL-12` link is an issue, not a page, and is
> ignored.

**Expect** — the reply carries:

```
What I could not read
  https://…/pages/999999/Gone — That Confluence page was not found, so its
  contents were not used.
```

**Read the reply, not the ticket list.** The point is that a missing
specification is announced rather than silently ignored.

### T14 · Work ruled out in a later comment

**Ticket:** `Notifications`, description
`Send email and SMS notifications, and let users manage preferences.`

**First comment** (an ordinary comment, no mention):

```
SMS is out of scope for this release, email only.
```

**Then:**

```
@Aetherion please break this down into stories
```

**Expect** — Stories for email and for preferences. **No SMS Story.**

### T15 · Ticket and page disagree

**Ticket:** `Saved cards`, description
`Customers may store one saved card per customer.`
Link a Confluence page saying `Customers may store up to five saved cards.`

**Comment:** `@Aetherion please break this down into stories`

**Expect** — the reply carries:

```
Conflicting information
  FL-nn says 1 card(s); "Payments Spec" says 5. The ticket was used.
  Check which is right.
```

The agent does not decide which is correct. It says which it used.

---

## Part 5 — Edge cases

### Attachments — the requirement lives in a file

Attach the file to the ticket, then comment `@Aetherion Build what the attached
file describes.`

| # | Attach | Expect |
| --- | --- | --- |
| **A1** | A `.pdf` stating one requirement | Breakdown built from the PDF's contents |
| **A2** | A `.csv` or `.xlsx` of rules | Breakdown built from the rows |
| **A3** | A `.docx` | Breakdown built from the document |
| **A4** | A `.json` such as `{"requirement": "Let drivers log a break", "max_minutes": 30}` | Breakdown built from it. **This was rejected as unsupported before v4.0.13** |
| **A5** | A `.json` that is truncated or corrupt | Nothing built from it. Reply says *"Could not read this attachment: the file is not valid JSON"* |
| **A6** | A `.zip` or `.exe` | Reply says the type is unsupported. The run still completes |
| **A7** | A screenshot (`.png`) of a form | Its contents are read. Slower — one model call per image |
| **A8** | A very long PDF (40+ pages) | Reply reports that characters were omitted **from the middle**. Both the opening and the closing sections must appear in the breakdown |

**A8 matters more than it looks.** Scope and acceptance criteria are almost
always at the end of a requirements document. Confirm the last section
influenced the output — not just the first.

### Confluence

| # | Setup | Expect |
| --- | --- | --- |
| **C1** | Link a page holding the requirement | Breakdown built from the page |
| **C2** | Link a page whose body says *"see the attached sheet"*, with a `.csv` attached to the page | Breakdown built from the **attached sheet** |
| **C3** | Link a page you cannot read | Reply lists it under *"What I could not read"* |
| **C4** | Link a page that contradicts the ticket | Reply has a *"Conflicting information"* section. Neither version is silently chosen |

### Duplicates worded differently

**D1 · The failure this was built for**

1. Comment `@Aetherion Let depot staff record a tyre pressure check.` — note what
   is created.
2. Wait, then comment **exactly the same text** again on a different ticket.

Expect: **nothing created**, and a reply naming the tickets from step 1.

This must hold *even when the two runs word the titles differently* — one run
may produce "Allow depot staff to record tyre pressure checks" and another
"Staff record a tyre pressure check". Matching on the requirement text is what
catches it. In production this exact case created a duplicate Story.

### Email discipline

| # | Do this | Expect |
| --- | --- | --- |
| **M1** | `@Aetherion who is the PM?` | **No email at all.** Not at any setting |
| **M2** | A vague but genuine request | Email: `Not created: 3 details missing` |
| **M3** | A repeat of existing work | Email: `Not created: this work already exists (matches FL-x)` |
| **M4** | A successful creation | **No email** unless `created` is in `LTW_NOTIFY_ON` |
| **M5** | Break `JIRA_API_TOKEN`, then trigger twice within 15 minutes | **One** email, not two. The second is suppressed as a retry |
| **M6** | Set `LTW_ADMIN_EMAILS`, then cause a failure | The failure goes to the admin address, not the team list |

Every subject line must state the outcome on its own. A subject reading only
*"could not be processed"* is a bug.

### Bad input — nothing should ever be created

| # | Comment | Expect |
| --- | --- | --- |
| **E1** | `@Aetherion What is the weather today?` | Out of scope, nothing created |
| **E2** | `@Aetherion Make the app better.` | Refused as unusable, asks for detail |
| **E3** | `@Aetherion` (nothing else) | Refused. Must **not** silently decompose the description |
| **E4** | `@Aetherion 🚗🚗🚗` | Refused |
| **E5** | `@Aetherion asdkjh asdkjh asdkjh` | Refused |
| **E6** | Paste 25,000 characters | Handled; reply says what was truncated |

### Security

**E7 · A forged call is refused**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  http://localhost:8000/jira/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Hub-Signature: sha256=deadbeef' \
  -d '{"webhookEvent":"comment_created","issue":{"key":"FL-1"}}'
```

Expect `401`. Also check a request with **no** signature gets `401`.

**E8 · A link to another Atlassian site is never fetched**

```
@Aetherion Build what this says: https://some-other-site.atlassian.net/wiki/spaces/X/pages/1/Y
```

Nothing from that host may appear anywhere. A ticket comment is untrusted input.

**E9 · A project outside the allow-list is refused**

Comment on a ticket in a *different* project. With
`LTW_ALLOWED_PROJECT_KEYS=FL`, the agent must refuse and create nothing there.

### Loops and repeats

**E10 · It never answers itself**

Watch after any successful run. The reply ends
`— AetherionAgent · automated reply`, and there is exactly **one** reply per
request.

**Fails if:** replies keep appearing. Stop the receiver immediately.

**E11 · Repeat deliveries collapse**

Re-trigger the same comment, or let Jira retry. The second delivery is ignored —
answered comments are recorded per comment, so one ticket can carry many
requests without replaying old ones.

### Failure handling

**E12 · Wrong credentials**

Temporarily break `JIRA_API_TOKEN`, restart, and comment. Expect a clear message
naming the credentials — not a stack trace, and no partial tickets.

**E13 · Read-only mode**

Set `LTW_READ_ONLY=true`, restart, and comment. Expect the full breakdown to be
produced and reported, with **nothing** written to Jira.

---

## Known limitations

Expected behaviour. Do not raise these as bugs.

| What you will see | Why |
| --- | --- |
| Sub-tasks always read `Define the rules for: X` / `Implement the behaviour for: X` / `Verify and hand over: X` | The AI model is unreachable, so the deterministic fallback runs. Correct and repeatable, but mechanical |
| A long detailed paragraph produces a run-on Story title | The fallback cannot tell a *rule* from a *capability*. Your numbers are preserved — put rules on lines below the request |
| `I want to a request moves through four states…` | Same cause: it restates your words rather than rephrasing them |
| Noun-phrase bullets give `I want to driver shift roster` | Write bullets as actions instead |
| Only the trigger ticket's comments and attachments are read | Other tickets contribute summary and description only |

The first four improve once the AI model is reachable.

---

## Results sheet

| # | Test | Created? | Reply headline | Pass |
| --- | --- | --- | --- | --- |
| T1 | One capability | | | |
| T2 | Two capabilities | | | |
| T3 | Six capabilities | | | |
| T4 | Bullet list | | | |
| T5 | Values not work | | | |
| T6 | Exact duplicate | | | |
| T7 | Partly new | | | |
| T8 | Asked twice | | | |
| T9 | Closed work | | | |
| T10 | Ticket description | | | |
| T11 | Question | | | |
| T12 | Confluence page | | | |
| T13 | Unreadable page | | | |
| T14 | Ruled out later | | | |
| T15 | Conflict | | | |
| E1–E6 | Bad input | | | |
| E7–E9 | Security | | | |
| E10–E11 | Loops | | | |
| E12–E13 | Failures | | | |
| A1–A8 | Attachments | | | |
| C1–C4 | Confluence | | | |
| D1 | Reworded duplicate | | | |
| M1–M6 | Email discipline | | | |

**A test that creates the wrong *number* of Stories is a failure even if each
Story reads sensibly.** The split is the hard part, and the part that has broken
before.

---

## Automated tests

Run these any time; they use a fake Jira and need no credentials.

```bash
uv run pytest -q                  # 853 tests
uv run ruff check src tests
uv run black --check src tests
```

---

## Deploying what you tested

Once these pass, see [PUBLISH.md](PUBLISH.md). Bump the version before
publishing — publishing the same version over itself may leave the old build
running.
