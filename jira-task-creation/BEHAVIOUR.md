# What the agent says

This page shows exactly what JiraTaskCreation writes back, in every situation.
Nothing here is a summary of intent — these are the real messages, taken from
the code that sends them.

If you only read one thing, read the table at the top of each section.

---

## The four outcomes

Every comment mentioning `@Aetherion` ends in exactly one of four outcomes.

| Outcome | What it means | Created in Jira | Reply comment | Email |
|---|---|---|---|---|
| **1. Not a requirement** | Chit-chat, a general question, an idea that isn't work | Nothing | Yes — one short line | **Never** |
| **2. Too thin to build** | Real work, but essential detail is missing | Nothing | Yes — with 2–3 questions | Yes |
| **3. Already exists** | The same work is already in the project | Nothing | Yes — with links | Yes |
| **4. Valid** | Enough to break down | Epic / Stories / Sub-tasks | Yes — with links | No, by default |

Two rules hold across all four:

- **There is always exactly one reply comment.** An email can be filtered or
  missed; the ticket cannot. Whatever happened, the ticket carries the record.
- **The agent never answers its own comment.** Every reply ends with the footer
  `— AetherionAgent · automated reply` and contains no `@Aetherion` mention, so
  it cannot trigger itself.

---

## Outcome 1 — Not a requirement

**When:** the comment contains no request for work. "Who is the PM of India?",
"I want to go home early", "thanks", "hi".

**Reply comment:**

```
AetherionAgent · Invalid request — this is not a work requirement

What happened
This is not a work requirement. Reply with a description of what should be
built or changed.

— AetherionAgent · automated reply
```

**Email: never.** Not configurable, not overridable. Treating idle chatter as
something worth alerting a person about is how a useful agent becomes one that
everybody mutes. This is the one rule no setting can change.

**No questions are asked.** Asking "which project is this for?" of someone
saying hello gets the situation backwards.

**An unfamiliar verb does not land here.** The deterministic verb list is
hand-maintained and has been wrong in production three times, each time
rejecting a real requirement — "Log a rest break.", "alert the depot manager
when a fault is reported". Words it still does not know include *paginate*,
*throttle*, *encrypt*, *expire*, *toggle* and *aggregate*.

So a comment with no recognised verb only lands in Outcome 1 when it is also
too thin to be a requirement, a question about the world ("who is the PM?"), or
someone talking about themselves ("I want to go home early"). Anything else —
readable, substantial, about the product — goes to the model to decide. If the
model cannot be reached it becomes Outcome 2 and is *asked about*, never
rejected: a needless question costs one comment, a wrong rejection loses a
requirement with no email, no question and no record that it happened.

---

## Outcome 2 — Real work, not enough detail

**When:** the comment names a genuine change but leaves out what's needed to
size it. "Add export to the reports page."

**Reply comment:**

```
AetherionAgent · More information is needed — nothing created

What happened
The request names a change but does not say enough to break it down.

Answers needed
• Who is this for — which role or type of user?
• What are the rules or limits? For example a format, a maximum, or when it
  should happen.
• How will you know it is done — what does success look like?

— AetherionAgent · automated reply
```

Only genuinely missing things are asked. A question about something the
requirement already answers reads as though nobody looked at it. At most three.

**Email subject:**

```
[JiraTaskCreation] FL-123 — Not created: 3 details missing
```

**Email body:**

```
Jira issue : https://yoursite.atlassian.net/browse/FL-123
Summary    : Reports page improvements
Project    : FL

SITUATION
The request names a change but does not say enough to break it down.

NO JIRA ISSUES WERE CREATED.

WHAT IS NEEDED
  - Who is this for — which role or type of user?
  - What are the rules or limits?
  - How will you know it is done?

WHAT TO DO NEXT
Answer the questions in a comment on the ticket, mentioning @Aetherion again.
```

---

## Outcome 3 — The work already exists

**When:** the same requirement was already broken down, or matching work is
already in the project.

**How a match is found — four independent checks:**

| Check | What it compares | Catches |
|---|---|---|
| **Idempotency label** | A fingerprint of the stated request + project | The same request asked again, in any comment |
| **Title similarity** | Proposed titles against existing summaries (0.75) | The same work, similarly worded |
| **Requirement text** | What the person typed, against existing summaries and descriptions (0.80) | The same work worded *differently* |
| **Model adjudication** | Pairs scoring 0.30–0.75, which word overlap cannot decide | The same work *reworded* |

The label is keyed on the **stated request**, not on the comment id and not on
the assembled ticket bundle. The comment id was wrong because the same request
asked a second time then got a different label, found nothing to reuse, and
built the whole hierarchy again. The bundle is wrong because it grows every
time anyone comments, so it never hashes the same way twice.

The fourth check exists because word overlap cannot tell a reword from a
sibling. "Record a rest break" against an existing "Allow drivers to log a rest
break" scores 0.40 — the same work, well under the line. "Send email
notifications" against "Send SMS notifications" scores 0.60 — different work,
also under the line. No threshold separates those two, so anything in the band
is put to the model, one short question per pair:

```
PROPOSED: Record a rest break
EXISTING (FL-108): Allow drivers to log a rest break
→ SAME    — blocked, nothing created

PROPOSED: Send SMS notifications to tenants
EXISTING (FL-85): Send email notifications to tenants
→ DIFFERENT — built as new work
```

It is deliberately conservative: anything the model does not positively confirm
is treated as new work, which is exactly what happened before the check
existed. If the model is unreachable, only word overlap runs. Switch it off
with `LTW_DUPLICATE_ADJUDICATE=false`.

**Reply comment:**

```
AetherionAgent · This work already exists — nothing created

Existing work this overlaps
• "Staff record a tyre pressure check" matches FL-116
  ("Allow depot staff to record tyre pressure checks", similarity 0.833)

— AetherionAgent · automated reply
```

**Email subject:**

```
[JiraTaskCreation] FL-123 — Not created: this work already exists (matches FL-116)
```

---

## Outcome 4 — Valid requirement

**When:** there is enough to work from.

**Size decides the shape:**

| Size | What is created |
|---|---|
| Small | One Story with Sub-tasks — no Epic |
| Medium | One Epic, Stories, Sub-tasks |
| Large | One Epic, more Stories, Sub-tasks |

**Reply comment:**

```
AetherionAgent · Work breakdown created.

What happened
1 Story and 3 Sub-tasks created successfully.

Created in Jira
• Story FL-120 — Staff record a tyre pressure check
• Sub-task FL-121 — Define the rules for: ...
• Sub-task FL-122 — Implement the behaviour for: ...
• Sub-task FL-123 — Verify and hand over: ...

— AetherionAgent · automated reply
```

**Email: not sent by default.** The comment on the ticket is the record, and a
successful creation needs no chasing. Add `created` to `LTW_NOTIFY_ON` to turn
it on; the subject then states the counts:

```
[JiraTaskCreation] FL-123 — 5 items created (1 epic, 2 stories, 2 sub-tasks)
```

---

## System failures

A failure is an operations problem — an expired token, a missing issue type, a
Jira API error — not a project update.

**Email subject** names the actual cause, never "something went wrong":

```
[JiraTaskCreation] FL-123 — Failed: Jira rejected the Sub-task type
```

**Who gets it:** `LTW_ADMIN_EMAILS` if set, otherwise `LTW_NOTIFY_EMAILS`.
Sending breakage reports to everyone who wanted to hear about their own
requirements trains the whole team to filter the agent out.

**Retries do not re-send.** A failing run is retried automatically, sometimes
for hours. An identical message is sent once and then suppressed for
`LTW_EMAIL_REPEAT_WINDOW` seconds (15 minutes by default). Without this, one
stuck ticket produces an email every few seconds.

---

## Extra sections that appear when relevant

These are added to the reply comment whenever they apply, in any outcome.

| Section | When it appears | Why it matters |
|---|---|---|
| **What I could not read** | An attachment or linked page failed | Otherwise a thin breakdown looks like a bad requirement, when really the spec was never opened |
| **Conflicting information** | A linked page contradicts the ticket | A linked page is usually newer; silently preferring one is what's wrong |
| **Problems** | Something went wrong but the run continued | — |
| **Notification** | An email was sent or could not be delivered | You can see whether the mail got out |
| **⚠ Please review the wording** | The model was unreachable and the fallback wrote the tickets | Appears first, above everything. Otherwise a reader takes the generic wording for the agent's normal quality instead of an outage. The same warning goes in the email |

---

## The one email setting

`LTW_NOTIFY_ON` — a comma-separated list, any of:

| Value | Sends email when | Default |
|---|---|---|
| `clarification` | Detail is missing | **on** |
| `duplicates` | The work already exists | **on** |
| `failed` | The run failed | **on** |
| `created` | Items were created | off |

`invalid` is not a value. Outcome 1 never emails at any setting.

The principle: **an email means a person has something to do.** Anything else
belongs on the ticket, where it can be read when someone looks.

---

## What the agent will not do

- It will **not implement** the requirement. It produces the work breakdown.
- It will **not name source files** it invented. If you name a file yourself,
  it may repeat it back; it will not make one up.
- It will **not invent scope**. Everything it writes is grounded in the ticket,
  its attachments, its comments and its linked pages.
- It will **not create duplicates**, and it will **not create anything** for
  outcomes 1, 2 or 3.

---

## GENERATE_LOCAL_PDF=true — a PDF by email, and nothing changed

**On the ticket:**
```
AetherionAgent · Work breakdown emailed as a PDF — no Jira changes made
What happened
  PDF only — no Jira tickets were created or changed. The breakdown proposes
  4 Stories and 8 Sub-tasks. If built, BGV-25 would become an Epic (it is a
  Task now). The full breakdown — every ticket, its criteria and the sources
  read — was emailed to kothakota.sailaxmanrao@calfus.com as
  aetherion-breakdown-BGV-25-ebbd771f.pdf.
Proposed — not created in Jira
  - S1  Generate Monthly Invoices for Completed Checks — Sub-tasks: S1.1 …, S1.2 …
Details I could not determine — please confirm
What I read
```

**The email** — subject `[Work Breakdown] BGV-25 — breakdown PDF: Monthly client invoicing`:
a yellow "No Jira tickets were created or changed" box, the summary, the proposed
tickets, the questions to confirm, and the PDF attached. If it cannot be sent,
the reply says so and nothing is created — asking again is safe.

---

## Delegated mode — what it says, and to whom

When the router invokes this agent (`mode="delegated"`), **it says nothing to
anyone**. No Jira comment, no label, no email. Everything below in this document
describes the webhook and manual paths only.

Instead it returns `shared.contract.AgentResult`, and the router composes the one
reply from it. The mapping from this agent's outcomes to what the reader sees:

| This agent concluded | Contract outcome | Headline the router shows |
|---|---|---|
| Created the hierarchy | `CREATED` | Work breakdown created |
| Found existing work, created nothing | `ALREADY_EXISTS` | This work already exists |
| Needs answers first | `NEEDS_INFO` | More information needed before this can be broken down |
| Not a requirement / out of scope | `NOT_A_REQUIREMENT` | Invalid request — this is not a work requirement |
| Answered a question, created nothing | `REVIEWED` | Nothing was created |
| Creation failed | `FAILED` | Could not complete this request |

**Emails in delegated mode.** The agent never sends one. It records what it
*would* have sent as `email_recommended` + `email_kind`, and the router decides.
An invalid request can never carry a recommendation — the mapping table has no
entry for it, and `NOT_A_REQUIREMENT` refuses one regardless. So "never email an
invalid request" holds on the routed path by construction, not by convention.

**Degraded runs.** When the model was unreachable and the local fallback wrote
the wording, the contract carries `degraded: true` with the reason. The router
places the "please review the wording" warning; this agent only reports it.
