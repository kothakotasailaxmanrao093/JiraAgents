# Questions you will be asked — and one-line answers

Read `CODE.md` first. Every answer here is true of the code on 29 Sep 2026 (router 0.1.26, Work Breakdown
4.3.42, Review 2.2.19). Say the **A:** line; use the rest only if pressed.

---

## A — Distinguished Engineer

**Q: How do you read .txt, .docx and .pdf? Which libraries?**
A: We don't parse them ourselves. We call the platform's `agent_lib.utils.file_utils` (`shared/attachments.py:200`), which uses PyMuPDF for PDF and python-docx for Word; plain text we decode as UTF-8 ourselves.
→ Why not our own parsers: the platform already covers .doc, .xls, .pptx and images; otherwise we'd pin and upgrade six parsers in three agents.
⚠ Trap: never say "OCR" or "Tesseract". Images go to the platform's **vision model**, which describes them.
Follow-up: "What about a 200-page PDF?"

**Q: Why the platform library and not PyMuPDF directly?**
A: One dependency owned by the platform instead of six owned by us, and the same behaviour in every agent.
→ Why not direct: we'd own every parser's upgrades and CVEs, three times over.
⚠ Trap: "We use PyMuPDF" invites "so you own PDF parsing?" Say: "the platform library owns it; we call one function per extension."
Follow-up: "What if file_utils changes?" → "A renamed function would show up as 'Could not read this attachment' on that file, not a crash, and all the calls are in one place (`attachments.py:202-222`)."

**Q: pdfplumber and pypdfium2 are in your venv — do you use them?**
A: No. They're installed by other packages; nothing in our source imports them.

**Q: What happens to a 200-page PDF?**
A: We keep 20,000 characters of it, two thirds from the start and one third from the end, and the whole requirement sent to the model is capped at 24,000 characters.
Follow-up: "And the eleventh attachment?" → "Only the first 10 are read, and the reply says so."

**Q: Why middle-out and not the first N characters?**
A: Scope, limits and acceptance criteria usually sit at the end of a spec; first-N kept the preamble and lost them (`attachments.py:136-139`).
→ Why not summarise with the model: that's an extra call per file, and it can drop the exact numbers we check for.

**Q: When the bundle is too big, what goes first?**
A: Whole sections, in a fixed order: work logs, history, activity, links, ticket, comments, attachments, and Confluence last; the request itself is never dropped (`ingest.py:33-44`).

**Q: How do you stop one comment creating tickets twice?**
A: The router records each answered comment id on the issue and ignores a redelivery before any model call; below that, an idempotency label on every ticket we create, and a comparison with the project's existing tickets.
Follow-up: "How many layers?" → "Six. They're listed in CODE.md §6."

**Q: Two people comment at the same second — what happens?**
A: Usually they are sequenced: each ticket records the job running on it, so the same request again says "already reviewing" and a different one waits its turn and then runs on that ticket.
⚠ Honest gap: "It is state in a Jira issue property, not a true lock — two comments in the very same second can both see the ticket as idle. Work Breakdown's own duplicate check stops a second identical build."

**Q: Your classifier is a model. What happens when it's wrong?**
A: A wrong BUILD needs 0.80 confidence and a wrong REVIEW 0.60; below that it asks "Which did you mean?", and saying `build` or `review` first skips the model entirely.
→ Why not a rules-only classifier: people write "let HR managers download the report as PDF" with no verb; only a model reads that as a build.

**Q: Why 0.80 for BUILD and 0.60 for REVIEW?**
A: A wrong build creates tickets someone must delete; a wrong review is one comment (`catalog.py:17-21`).
⚠ Honest gap: "The numbers are judgement, not tuned on a dataset. We don't have a labelled set yet."

**Q: Why a child workflow and not an HTTP call between agents?**
A: The router must wait for the child's result to post the one reply; Temporal gives that with retries, timeouts and a linked run history.
→ Why not HTTP: we'd rebuild retries, timeouts and tracing, and lose the parent/child link in the run view.
Follow-up: "Costs?" → "A child whose worker is down waits for the full timeout (7 min build, 4 min review), and the retry policy doesn't shorten that (`router_flow.py:288-302`)."

**Q: What happens when the AI gateway is down?**
A: Nothing is created and the reply says so. There is no fallback that writes generic tickets (`decompose.py:1701`), and the router asks "Which did you mean?" instead of guessing.
→ Why no fallback: the old fallback wrote boilerplate tickets that looked real; the owner removed it on 25 Sep 2026.

**Q: How do you stop the agent replying to itself?**
A: Every reply is signed `AetherionAgent` with a fixed footer, and ingress ignores any comment carrying it; the footer has no bare "@Aetherion" mention.
Follow-up: "And by account?" → "There's an account check too, but only when `ORCH_BOT_ACCOUNT_ID` is set."

**Q: Someone writes "ignore your instructions" in a ticket — what happens?**
A: Honestly, it's untested (`PHASES.md:342`); the text goes into the prompt as-is. What limits it: the router only accepts four intents, every AI answer is schema-validated with unknown fields rejected, and writes are limited to FL and BGV.
⚠ Trap: don't say "we sanitise the input". We don't.

**Q: What is untested?**
A: Prompt injection, two comments racing, load, and the new root conversion through a real comment; unit tests are 294 + 1081 + 236, all against fakes, with live checks by hand (`tests.md`, `DemoTest.md`).

**Q: Where does it break first at 100× traffic?**
A: The AI model calls: up to 9 per build, each seconds long, plus Jira's API rate limits. I'd measure before guessing further.
⚠ Honest gap: "We have no load test."

**Q: Two weeks to refactor — what?**
A: A per-issue lock, a prompt-injection test set, metering tokens per run, moving Review to SDK 0.0.83, and deleting the dead `heuristic_breakdown` code.

**Q: How many model calls in one build?**
A: Usually two (breakdown and self-review — a clear 3+-line requirement skips triage), at most about five; review is one, routing is zero with a verb or one without, and a review of an unchanged ticket is zero (reused).

**Q: Why not one agent that does everything?**
A: Build writes to Jira and review must not; separate agents keep the permissions and failure modes apart, and a fourth agent is a catalog entry.

---

## B — Senior CTO

**Q: What problem does this solve, and what did it cost before?**
A: Turning a written requirement into well-formed Jira tickets and spotting gaps before a sprint starts. That was done by hand by a BA or lead; I don't have a measured hours figure.

**Q: How much does one run cost?**
A: One to nine model calls. We don't meter tokens per run yet, so I'd have to take the figure from the gateway's usage view.
⚠ Trap: don't invent a rupee or dollar figure.

**Q: Blast radius if it goes wrong?**
A: At worst it creates or changes tickets in FL and BGV only. It never deletes, and it never touches comments, attachments or links; the ticket's original text is kept under "Original request".

**Q: What data leaves our environment, and to whom?**
A: Ticket text, comments, attachment text and Confluence text go to the Aetherion AI Gateway, which calls OpenAI models (gpt-4o, gpt-5.1); failure emails go out through Gmail SMTP.
Follow-up: "Credentials?" → "They're in each agent's `.env`, which ships inside the published package. It's not in git, but anyone who can read the package can read them."

**Q: Could it write to a production project by mistake?**
A: Only FL and BGV are allowed, checked in the router and again before every write (`ORCH_ALLOWED_PROJECT_KEYS`, `LTW_ALLOWED_PROJECT_KEYS`).

**Q: What if the platform changes its API?**
A: It already happened: graphql-core 3.3 crashed the worker pod on 28 Sep, and we pinned `<3.3` the same day. We're exposed to the platform's SDK and image, and pin what breaks.

**Q: Build vs buy?**
A: Off-the-shelf Jira AI summarises tickets; this one creates a checked hierarchy, carries every stated number into acceptance criteria, and refuses duplicates. I haven't done a formal comparison.
⚠ Honest gap: say "I haven't evaluated Atlassian Intelligence against it" if you haven't.

**Q: What does it NOT do that people will assume?**
A: It doesn't estimate effort, assign people, set sprints unless asked, edit any ticket except the one it was asked on, or understand images beyond what the vision model describes.

**Q: How do we turn it off in a hurry?**
A: Disable the Jira webhook (Jira → System → WebHooks); that's instant. The read-only env switches need a republish.
⚠ Trap: `ORCH_READ_ONLY` only stops the router posting replies, not the child creating tickets.

**Q: Maintenance burden in six months?**
A: Platform SDK upgrades (three agents), the verb and fact-check lists, the model's quality drifting, and the webhook secret. About a day a month, if nothing else changes.

---

## C — Senior Director

**Q: How do we know it's working?**
A: Two numbers per run: **avoidable** (facts dropped by the build) and **over-reach** (findings the review invented), recorded by hand in `tests.md`. There's no dashboard yet.

**Q: How much time does it save?**
A: Not measured yet. A 22-line requirement becomes an Epic with its Stories and Sub-tasks in 3–4 minutes; the honest next step is timing a BA doing the same.

**Q: What if a team doesn't trust its tickets?**
A: Every reply lists what it read, what it couldn't read, and "details I could not determine — please confirm"; nothing is hidden, and the review agent checks the build.

**Q: Who owns this when you leave?**
A: That needs deciding. The docs (`HOW_IT_WORKS.md`, `CODE.md`, `tests.md`) are written for someone who didn't build it.

**Q: How does a new team start?**
A: Add their project key to the two allow-lists, add it to the webhook filter, republish, and they comment `@Aetherion help`.

**Q: Rollout plan?**
A: Today it's two test projects; next is one real team's project, with the webhook limited to it, running for two sprints with the two quality numbers tracked.

**Q: What's gone wrong so far?**
A: Two days of silence from a webhook secret mismatch, tests that passed on invented data, a pod crash from a library release, and nine duplicate Sub-tasks from one ticket asked three times. Each one now has a test or a runbook line.

**Q: What would you build next?**
A: A routing log (why a comment went where), token metering, and a per-issue lock.

---

## D — Technical Delivery Manager

**Q: How long does one request take?**
A: Help about 30 s, review 1–2 min, a build 2–4 min; worst case the timeout, 7 min for a build and 4 for a review.

**Q: What do users see when it fails?**
A: A reply on the ticket saying what failed and what to do, for example "Could not read this ticket — nothing was done"; a failure is never silent after the webhook reaches us.
⚠ Trap: a wrong webhook secret *is* silent. It's rejected before our code runs.

**Q: Who gets alerted?**
A: The address in `ORCH_NOTIFY_EMAILS`, by email, for clarification, duplicates, failures and a reply that couldn't be posted. There is no pager.

**Q: How do you deploy and roll back?**
A: `scripts/publish.py` runs tests → lint → version bump → `uv sync` → `aetherion publish`. To roll back, republish the previous version from source; there's no one-click rollback.
⚠ Honest gap: the git repo has one commit and today's work is uncommitted, so tag every published version.

**Q: How do we know it's up right now?**
A: Argo CD shows the pods healthy, Agent List shows the versions, and a `health_check` payload dispatches each agent and tests the SMTP login.

**Q: What manual steps still exist?**
A: Copying each agent's task-queue id into the router's `.env`, setting the webhook secret, re-logging into Aetherion when the login expires, and publishing the router first when the contract changes.

**Q: On-call runbook?**
A: Check in this order: webhook secret → Event Subscription → Agent List versions → Temporal run for that time → the reply (`HOW_IT_WORKS.md` §5).

**Q: How do you test before release?**
A: The publish script refuses to upload unless all unit tests and lint pass; then the live cases in `tests.md` on the BGV project.

**Q: Jira down for an hour?**
A: Nothing runs, because the webhook comes from Jira; comments made during the outage are not replayed.
⚠ Honest gap: "there's no replay queue."

**Q: Debug "a user says it did nothing"?**
A: Get the ticket key and the time, then check the webhook secret first: every comment silent means a secret mismatch. After that, find the Temporal run for that minute.

---

## E — Project Manager

**Q: What's done and what isn't?**
A: Done: routing, build, review, explain, help, reading every source, root conversion, threaded replies, email. Not done: lock, routing log, token metering, the prompt-injection test, and Review's SDK upgrade.

**Q: What are you blocked on?**
A: Platform items: the worker image's graphql-core pin, and SDK downloads that only work on the office network.

**Q: Biggest risk to the timeline?**
A: The platform: a library release crashed our pod today, and registrations sometimes lag after a publish.

**Q: What didn't you build that we agreed?**
A: The routing log (D4); the lock was agreed and then removed by the owner's decision.

**Q: How long to add a fourth agent?**
A: The router side is one catalog entry and a task-queue variable; the agent itself depends on what it does.

**Q: Who else needs to be involved?**
A: The platform team (SDK, image, secrets), a Jira admin (webhook, projects), and security (data sent to the model).

**Q: What would production-ready need?**
A: A real project behind the allow-list, secrets out of the packaged `.env`, a lock, a routing log, alerting beyond email, and tagged releases for rollback.

**Q: What's the demo?**
A: `DemoTest.md` Set 3: one ticket with a description, a Word file, a transcript and a Confluence page becomes an Epic with every fact carried, then gets reviewed, and a production problem becomes a Highest-priority Bug.

---

## Questions YOU should ask

**Ask the CTO**
1. "Which of the not-built items would stop you putting this on a real project?" — it turns the gap list into their priority, not yours.
2. "Is sending ticket content to the AI gateway acceptable for every project, or only some?" — it's their risk to own, and the answer changes the rollout.
3. "If we had half the time, what would you cut?" — it shows you think in trade-offs.

**Ask the Director**
4. "What would convince you it saves time — a timed comparison, or team feedback?" — it defines the metric before you collect it.
5. "Which team would be the right first real user?" — adoption is theirs to unlock.
6. "Who should own this after me?" — it raises ownership before it becomes a problem.

**Ask the Distinguished Engineer**
7. "Is a per-issue lock worth it here, or is the duplicate check enough?" — it invites judgement on a real gap you already know about.
8. "What have you seen fail in systems that let a model decide what to write?" — it draws on experience you don't have.
9. "Is the 0.80 / 0.60 split the right bar, or should we measure it first?" — it asks whether the quality bar is right, not only whether you hit it.

**Ask the Delivery Manager**
10. "What does on-call need from this to accept it — a pager, a dashboard, a runbook?" — it defines "done" operationally.
11. "Should releases be tagged and rolled back through a pipeline instead of by hand?" — it names your weakest operational point before they do.

**Ask the Project Manager**
12. "What does done look like for this phase — the demo, one live team, or production?" — it avoids scope drift.
13. "Who else should be in the room next time — security, the platform team?" — it pulls in the dependencies early.
