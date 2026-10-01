# Orchestration and local PDF — manual tests

Versions: router **0.1.27**, Work Breakdown **4.3.52**, Review **2.2.22** · project **BGV**

**Before you start:** Agent List shows those versions. In `jira-task-creation/.env`,
`GENERATE_LOCAL_PDF=false` for tests 1–6 and `true` for test 7 (publish Work Breakdown
after changing it).

---

### 1. Review → an answer at once, then the result in the same reply
**Ticket:** new Story, Summary `Employer verification`, Description `Officer contacts the employer and marks the result.`
**Comment:** `@Aetherion review`
**Pass:**
- Within seconds, a reply `⏳ Reviewing BGV-…` appears, and the card label is `aetherion-reviewing`
- Within about 2 minutes **that same reply** changes to the review (readiness score and findings), and the label becomes `aetherion-reviewed`
- There is only one reply comment

### 2. Build while the review is still running (your BGV-32 case)
**Ticket:** new Story like test 1. Comment `@Aetherion review`, and **straight away** comment `@Aetherion build`
**Pass:**
- The build's reply appears at once: `⏳ Build queued for BGV-… — waiting for the review … that started at HH:MM UTC`
- When the review finishes, the build starts by itself, **on the same ticket**
- If the review scores it **below 3/5**, the queued reply becomes `Build paused — the review scored BGV-… N/5` with the review's questions, and **nothing is created**. At 3/5 or more, it builds, with the review's points under "please confirm".

### 3. Build after a good review
**Ticket:** new Task, Summary `Referee reminders`
**Description:**
```
Recruiters chase referees who have not answered.
- If a referee has not replied 3 working days after the request, they get a reminder email
- At most 2 reminders per referee
- The recruiter sees "Reminder sent" with the date on the case
```
**Comment:** `@Aetherion review`, wait for the result (3/5 or more), then `@Aetherion build`
**Pass:**
- `⏳ Building …` at once, then `BGV-… is now a Story` in the same reply
- "please confirm" lists the review's open points as `From the review: …`
- The label is `aetherion-built`

### 4. The same request twice
**Comment:** `@Aetherion review` twice quickly, on a new ticket
**Pass:** the second reply says `⏳ Already reviewing BGV-… (started …) — nothing new was started`. Only one review runs.

### 5. Review an unchanged ticket again
On the test 1 ticket, after its review has finished, comment `@Aetherion review` again.
**Pass:** the same result comes back quickly, with `The ticket has not changed since the last review, so that review is shown again`.
**Then:** edit the description (add a line), and review again. It runs a **fresh** review.

### 6. A paused build runs after the ticket is fixed
On the test 2 ticket, add the answers to the description, then comment `@Aetherion build`.
**Pass:** it builds straight away. The old low review no longer counts.

### 7. Local PDF by email (`GENERATE_LOCAL_PDF=true`)
**Ticket:** new Task with the test 3 description
**Comment:** `@Aetherion build`
**Pass:**
- The reply `Work breakdown emailed as a PDF — no Jira changes made` names your email address and lists `S1 …`, `S1.1 …`
- **Your inbox** has `[Work Breakdown] BGV-… — breakdown PDF: …` with `aetherion-breakdown-BGV-…-xxxxxxxx.pdf` attached. Open it: the At-a-glance box, contents, every ticket with its criteria, and page numbers
- The ticket is **unchanged**, with **no attachment** and **no Sub-tasks**
- Comment `@Aetherion build` again right away. **No second email** arrives, because it's the same breakdown

### 8. Help lists everything
**Comment:** `@Aetherion help` → the list of commands. There's no "processing" reply, because help answers at once.

---

## Results

| # | Test | Pass / Fail | Ticket key | Notes |
|---|---|---|---|---|
| 1 | Review, same reply | | | |
| 2 | Build during review (BGV-32 case) | | | |
| 3 | Build after good review | | | |
| 4 | Same request twice | | | |
| 5 | Unchanged ticket reused | | | |
| 6 | Paused build after fix | | | |
| 7 | Local PDF | | | |
| 8 | Help | | | |
