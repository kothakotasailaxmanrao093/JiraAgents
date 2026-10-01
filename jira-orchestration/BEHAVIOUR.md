# What it says, word for word

Every reply this router posts, in full. If the system says something not in this
document, one of the two is wrong.

**Where the reply goes.** Under the comment that asked, in its thread, the way
Jira's own "Reply" does. If that comment is itself a reply, the answer goes in
the same thread (Jira threads are one level deep). If Jira refuses the thread,
the reply is posted as an ordinary comment instead, and the log says so.

---

## The envelope — in every reply, no exceptions

```
AetherionAgent · <headline>

Handled by
Jira Orchestration → <Display Name>

Routed to
<one sentence, naming the layer that decided>

<… outcome-specific sections …>

— AetherionAgent · automated reply
```

Four rules, each load-bearing:

- **"Handled by" names no child unless that child completed.** A chosen-but-dead
  agent gets `Jira Orchestration` alone — no arrow, no name.
- **The `run_id` never appears in a rendered comment.** It is an internal
  correlation id with no meaning to a Jira user; it still ties the dispatch
  call, the log lines and the admin email to one run, but only there. See
  [FLAWS.md](../FLAWS.md) F24.
- **"Routed to" and "Handled by" always name a display name, never a raw
  registered agent id** (`Task Creation`, never `JiraTaskCreation`). One table,
  `catalog.py::display_name_for`, is consulted on every path, including
  failure — see F24.
- **The footer can never retrigger the system.** `AetherionAgent` is spelled so
  the whole-word matcher for `Aetherion` cannot fire on it.

---

## One ticket, one job at a time — answered the moment you ask

Every **build** or **review** is answered at once with a "processing" reply, and
that same comment is **edited into the result** when the job ends — still one
reply per comment. The ticket remembers what is running on it and the last
review and build (issue property `aetherion-orchestration`), and the card shows
one status label at a time (`ORCH_STATUS_LABELS`): `aetherion-reviewing` →
`aetherion-reviewed`, `aetherion-building` → `aetherion-built`,
`aetherion-pdf-ready`, `aetherion-needs-input`.

**At once, when a job starts:**
```
AetherionAgent · ⏳ Reviewing BGV-32…
What happened   Started. The result will replace this message — usually within 2 minutes.
```

**A build asked while a review runs** (it waits, then runs on the same ticket and uses the review):
```
AetherionAgent · ⏳ Build queued for BGV-32
What happened   Waiting for the review of BGV-32 that started at 10:00 UTC to finish. This build
                will then start by itself, on BGV-32, and its result will replace this message.
```

**The same job asked twice** — nothing new starts:
```
AetherionAgent · ⏳ Already reviewing BGV-32
What happened   A review of BGV-32 started at 10:00 UTC is still running. Its result will appear
                in that reply — nothing new was started.
```

**A review of a ticket that has not changed** since the last review: the last
result is shown again, with "The ticket has not changed since the last review,
so that review is shown again; no new review was run." No model call.

**A build after a poor review** (below `ORCH_BUILD_MIN_READINESS`, default 3) of
the unchanged ticket — nothing is created:
```
AetherionAgent · Build paused — the review scored BGV-32 2/5
What happened   The last review of BGV-32 scored it 2/5, below the 3/5 needed to build, and the
                ticket has not changed since. Nothing was created. Answer these in the
                description (or attach them), then ask again — an edited ticket builds straight away:
                - How is the employer contacted?
                - What results can the officer mark?
```
After a review of 3/5 or more, the build runs and the review's open points
appear under "Details I could not determine — please confirm" as
"From the review: …". "Unchanged" means the summary, description and
attachments are the same — the agent's own replies and labels do not count.

If the "processing" reply cannot be posted at all, the job is **not started**
(the administrators are emailed): work nobody is told about is worse than
asking again.

---

## Work breakdown created

```
AetherionAgent · Work breakdown created

Handled by
Jira Orchestration → Work Breakdown

Routed to
work breakdown — the comment began with the verb "build" (Layer 1).

What happened
Created 1 Story and 3 Sub-tasks. Compared against 111 existing tickets in FL;
nothing matched.

Created in Jira
• Story BGV-41 — Raise an employment verification request per employer
• Sub-task BGV-42 — Create one request per employer with independent status
• Sub-task BGV-43 — Flag after 5 working days with no response

Details I could not determine — please confirm
Nothing was missing — every detail came from your description.

What I read
• FL-120 description
• 3 comment(s)
• Attachment: spec_v1.pdf

— AetherionAgent · automated reply
```

**"Details I could not determine — please confirm"** is on every build (D1,
2026-09-25). It lists each Story's open questions — decisions no source made,
asked and never invented — plus "Please check: …" for any avoidable finding the
self-review gate could not fix in its one regeneration. When the list is empty
it says "Nothing was missing — every detail came from your description."

**When the AI model is unavailable, nothing is created** (owner decision,
2026-09-25): the reply is "Could not complete this request" with the model
error under Problems. There is no fallback that writes generic tickets.

---

## Requirement review — nothing created

```
AetherionAgent · Requirement review — nothing created

Handled by
Jira Orchestration → Requirement Review

Routed to
requirement review — the comment began with the verb "review" (Layer 1).

Readiness
Needs major clarification (2/5). The card names the capability but states no
acceptance criteria and no error behaviour.

Findings (6)
• Ambiguity: "real-time" is used with no latency target given.
• Missing acceptance criterion: no criterion states what happens when the
  reading is out of range.
…

What I read
• FL-130 description and 4 comments
• Parent FL-100 description
• Attachment: requirements_v3.docx
• Confluence: "Tyre Pressure Logging — Spec v2"

What was missing or unreadable
• FL-130 has no acceptance criteria — please add them, so it is testable and
  two developers build the same thing
• spec_matrix.xlsx could not be read (password-protected) — please check the
  file opens, and re-attach it if it is corrupt or protected

— AetherionAgent · automated reply
```

**No Notification line.** A review created nothing, so it has nothing to
announce, and it never recommends an email.

**Requirement Review never produces `NEEDS_INFO`, even when there is nothing
to read.** A ticket with no description, attachment or linked page still gets
this same "Requirement review" shape — the gap appears under "What was missing
or unreadable" like any other. `NEEDS_INFO` (asking 2-3 blocking questions
before a build) is exclusively Work Breakdown's outcome — see
[FLAWS.md](../FLAWS.md) F26.

---

## A close call between build and review does not ask (Layer 2)

**Changed — see [FLAWS.md](../FLAWS.md) F27.** When the classifier is torn
specifically between BUILD and REVIEW (together holding at least 0.75 of its
confidence), the router no longer asks — it runs REVIEW, the reversible
guess, and says so:

```
AetherionAgent · Requirement review — nothing created

Handled by
Jira Orchestration → Requirement Review

Routed to
requirement review — build scored 0.41 and review scored 0.44 — neither
reached its own threshold, so the reversible action ran rather than asking
(Layer 2).

…

— AetherionAgent · automated reply
```

## Which did you mean? (Layer 3)

Reached only when the classifier's confidence is genuinely low on **both**
BUILD and REVIEW (below the combined floor above), or split some other way
entirely. Nothing is created, nothing is dispatched, no email.

```
AetherionAgent · Which did you mean?

Handled by
Jira Orchestration

Routed to
unclear — the classifier scored build 0.30, review 0.25, so nothing was run
rather than guessing (Layer 3).

I can do either of these — reply mentioning the agent again with:
• "@Aetherion build" — I create the Epic, Stories and Sub-tasks
• "@Aetherion review" — I assess how clear it is and list what is missing,
  creating nothing

— AetherionAgent · automated reply
```

---

## Invalid request — not a work requirement

**No email. Not configurable.**

```
AetherionAgent · Invalid request — this is not a work requirement

Handled by
Jira Orchestration

Routed to
not a work request — the comment asks about the world, not about this ticket
(Layer 2, chatter, confidence 0.96).

Mention the agent again with either a description of what should be built, or a
request to review this ticket.

— AetherionAgent · automated reply
```

---

## What I can do (help)

```
AetherionAgent · What I can do

Handled by
Jira Orchestration

Routed to
a request for help — listing what this system can do.

• "@Aetherion build <requirement>" — break a requirement into Jira issues
• "@Aetherion review" — assess this ticket and report what is missing
• "@Aetherion explain" — describe what this ticket is asking for
• "@Aetherion help" — show this list

— AetherionAgent · automated reply
```

---

## "@Aetherion explain this" / "what is this" — dispatched, not answered generically

**Changed — see [FLAWS.md](../FLAWS.md) F25.** The router used to answer these
itself with a fixed sentence. It now dispatches to Work Breakdown, which has
its own gate that detects a question on the trigger comment and answers it
using the actual ticket content, creating nothing:

```
AetherionAgent · About FL-120

Handled by
Jira Orchestration → Work Breakdown

Routed to
work breakdown — a question about this ticket — answered without creating
anything (Layer 1).

Answer
…a plain-English explanation written by the model from the ticket, its
attachments and linked pages; if the model is unavailable, the ticket's own
text quoted back…

— AetherionAgent · automated reply
```

Outcome `ANSWERED` (added 2026-09-24 — it used to travel as REVIEWED, so the
answer sat under "Readiness" headed "Nothing was created"). No email.

---

## Could not complete this request (child unreachable)

**"Handled by" names NO child — none ran.** The real exception is always in
**Problems**, never flattened to a generic sentence — see [FLAWS.md](../FLAWS.md)
F23. **Notification** says only what actually happened to the admin alert —
see the table under "Emails" below.

```
AetherionAgent · Could not complete this request

Handled by
Jira Orchestration

Routed to
work breakdown — the comment began with the verb "build" (Layer 1) — but that
agent could not be reached.

The work breakdown agent did not respond. It may not have finished, so check
the ticket before asking again.

Problems
• Dispatch to the Work Breakdown agent failed: RuntimeError: workflow type not
  registered

Notification
Could not email the administrators — this was logged instead.

— AetherionAgent · automated reply
```

For an agent that does **not** write to Jira, the "did not respond" line
instead reads *"Nothing was created, so asking again is safe."* The distinction
matters: it is the only thing telling the user whether a retry is safe. If no
admin address is configured, the **Notification** line is omitted entirely.

---

## A child ran and reported its own failure — different from "did not respond"

**Changed — see [FLAWS.md](../FLAWS.md) F28.** The section above is for a
child that could not be *reached at all* — the router's own dispatch call
failed. This is the other shape: the child ran, read some of its sources, and
then failed partway through (outcome `FAILED` in the contract it actually
returned). "What I read" and "What was missing or unreadable" now render
here too, whichever it managed before failing — they used to be silently
dropped for this outcome:

```
AetherionAgent · Could not complete this request

Handled by
Jira Orchestration → Work Breakdown

Routed to
work breakdown — the comment began with the verb "build" (Layer 1).

What happened
The AI Gateway crashed partway through.

What I read
• FL-120 description
• 2 comment(s)

What was missing or unreadable
• spec.docx could not be opened (corrupt file) — please re-attach it

Problems
• GatewayError: 503

— AetherionAgent · automated reply
```

---

## Degraded runs

When the child reports the model was unreachable, this appears **above**
"Handled by", so it is read before the content it warns about:

```
⚠ Please review the wording. The model was unreachable, so this was generated
from a local fallback and reads more woodenly than usual. (AI Gateway unreachable)
```

The child sets the flag; the router places the warning.

---

## When it says nothing at all

Silent, with no comment and no email — **exactly these, and nothing else**
(`routing/ingress.py::IgnoreReason`, a closed set; D0, 2026-09-25):

| `IgnoreReason` | Situation | Why silent |
|---|---|---|
| `NO_KEYWORD` | The comment does not mention the trigger keyword | Most comments are people talking to each other |
| `SIGNATURE` | The comment carries this system's signature | Answering it would loop forever |
| `OWN_ACCOUNT` | The comment was written by the bot account | Same |
| `DUPLICATE` | The comment has already been answered | Jira redelivers; the original reply stands |
| `OUTSIDE_PROJECT` | The issue is outside `ORCH_ALLOWED_PROJECT_KEYS` | Not this deployment's project |
| `NO_ISSUE` | The event carried no issue key | There is nowhere to reply |
| `NO_COMMENT` | The issue has no comments to answer | There is nothing to reply to |

Anything else that stops a run — Jira refusing the read, a network failure, any
tool raising — is **not** silence. It gets the reply below.

## Could not read this ticket

Posted when ingress fails (`outcome: "failed"`) or the ingress tool itself
raises. The administrators are alerted first, so the Notification line is true.

```
AetherionAgent · Could not read this ticket — nothing was done

Handled by
Jira Orchestration

Routed to
Not routed — the request could not be read, so it was never classified.

What happened
I received your request but could not read BGV-50. Nothing was created and
nothing was changed, so asking again is safe once this is resolved.

Problems
• Jira returned 403 Forbidden for BGV-50. This usually means the agent's Jira
  account does not have Browse Projects permission on this project. Jira
  returns the same error whether a ticket is missing or merely invisible to
  this account, so check the permission first.

Notification
Emailed the administrators.

— AetherionAgent · automated reply
```

The Problems line per cause (`tools.py::explain_read_failure`): 403 as above;
404 "may have been deleted or moved, or this account cannot see it"; 401 "the
API token may have expired"; 5xx "usually temporary; asking again in a few
minutes is safe"; a network error names it. No label is stamped and the comment
is not recorded as answered, so asking again works. A permissions failure will
often block this reply too; then the administrators' alert is the only signal.

A redelivery is logged with the **original** `run_id`, so it can be traced to
the run that answered it:

```
run 4a81f7c2: duplicate delivery for comment 10293, ignored
```

---

## Emails

**The router sends the one email per run**, through the same Gmail sender the
work-breakdown agent uses (`shared/mailer.py`). Children never send when the
router called them. A reply's Notification line says only what really happened
— "Emailed …" appears only when the email was actually accepted for delivery.

| Outcome | Email? | Notification line in the reply |
|---|---|---|
| Created | yes, if `created` is in `ORCH_NOTIFY_ON` | `Emailed <addresses>.` |
| Needs info | yes, if `clarification` is in `ORCH_NOTIFY_ON` | `Emailed <addresses>.` |
| Already exists | yes, if `duplicates` is in `ORCH_NOTIFY_ON` | `Emailed <addresses>.` |
| Failed (the child ran and failed) | yes, if `failed` is in `ORCH_NOTIFY_ON` | `Emailed <addresses>.` |
| Reviewed | **never** — nothing for a person to act on | none |
| Invalid request / not a work requirement | **never**, whatever any setting says | none |
| Help, chatter, question answered by the router, ambiguous | **never** | none |
| Child unreachable | admin alert | `Emailed the administrators.` when sent; otherwise `Could not email the administrators — this was logged instead.` |

When an email was wanted but did not go out, the reply says so and why:

- `No email was sent — Email not sent: GMAIL_SENDER or GMAIL_APP_PASSWORD is not set.`
- `No email was sent — Gmail rejected the credentials (535). Use a 16-character app password …`
- `Not emailed again — the same email was sent a few minutes ago.` (a retry within
  `ORCH_EMAIL_REPEAT_WINDOW`)

An outcome left out of `ORCH_NOTIFY_ON` is skipped silently — that is
configuration, not a failure. `ORCH_NOTIFY_ON` uses the same four names as the
work-breakdown agent's `LTW_NOTIFY_ON`. Both are set to
`clarification,duplicates,failed` — the owner asked for no email on creation
(2026-09-24), so a created breakdown's reply carries no Notification line.

The email: subject `[Aetherion] <ticket> — <headline>`; body with the headline,
a link to the ticket, the created keys / questions / existing matches / problems,
and the run id. Admin alerts: subject `[Aetherion] ALERT — <what failed>`.
