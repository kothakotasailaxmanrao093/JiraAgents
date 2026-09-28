# Demo test sets — 3 sets

| Set | Use it for | Topic | Steps |
|---|---|---|---|
| **1** | your own testing, before any demo | Address verification | 17 |
| **2** | a short demo | Education verification | 10 |
| **3** | the main demo | Police clearance check | 15 |

Each set has its own topic, so the sets never block each other as "already exists".
Run a set **in order**: some steps use tickets made by earlier steps.

Project **BGV** on jiraagentdemo.atlassian.net · Work Breakdown **4.3.40** · router **0.1.24** · Review **2.2.18**

## Before you start

1. **Agent List** shows the versions above.
2. **One new ticket per step** unless the step says otherwise. Create it as **Story** unless it says **Task**.
3. Write every comment in **Add a comment**, except where a step says **Reply**.
4. Wait the time given, then refresh the page. Big builds take 3–4 minutes.
5. The agent now **changes the ticket you comment on** into an Epic, Story, Task or Bug. Open an Epic by its key (`/browse/BGV-…`); the list view hides Epics.

## Every reply must

- sit **under your comment** (in its thread), one reply per comment
- start `AetherionAgent · …` and end `— AetherionAgent · automated reply`
- email only for **questions, already exists, failed**, never for a build that worked or a review

## Files and pages (already created)

| Set | Word file | Transcript | Confluence page |
|---|---|---|---|
| 1 | `test-files/Address_Verification_Spec.docx` | `test-files/Address_Meeting_Transcript.txt` | https://jiraagentdemo.atlassian.net/wiki/spaces/PR/pages/5308417 |
| 2 | `test-files/Education_Verification_Requirements.docx` | `test-files/Education_Meeting_Transcript.txt` | https://jiraagentdemo.atlassian.net/wiki/spaces/PR/pages/5341185 |
| 3 | `test-files/Police_Check_Spec.docx` | `test-files/Police_Check_Meeting_Transcript.txt` | https://jiraagentdemo.atlassian.net/wiki/spaces/PR/pages/5373953 |

---

# SET 1 — Testing · Address verification

### 1.1 Help
**Ticket:** BGV-1 (`Agent test ticket`) · **Comment:** `@Aetherion help` · **Wait:** 30 s
**Expect:** `What I can do`, listing build, review, explain and help. No email.

### 1.2 Build from four sources → the ticket becomes an Epic
**Ticket:** new Story, Summary `Address verification`
**Description:**
```
Verify the candidate's current home address.
- Candidate enters their current address and how long they have lived there
- A field agent visits the address and uploads a photo of the front door
- The policy is here: https://jiraagentdemo.atlassian.net/wiki/spaces/PR/pages/5308417
- More rules are in the attached spec and the meeting transcript.
```
**Attach:** `Address_Verification_Spec.docx` and `Address_Meeting_Transcript.txt`
**Comment:** `@Aetherion build` · **Wait:** 3–4 min
**Expect:**
- Headline `BGV-… is now an Epic`. Stories and Sub-tasks sit **under your ticket**, and **no other Epic** is created
- Your ticket's description is structured, and your own text is kept under **Original request**. The attachments and comment are still there
- **What I read** lists the description, both files and the Confluence page
- These facts appear in the Stories:

| From | Facts |
|---|---|
| Description | address + how long lived there · door photo |
| Confluence | last **12 months** only · photos kept **1 year** · never verify a **family member** |
| Word file | visit within **5 working days** · photo with **GPS** · at most **2 more visits** · **Verified / Not Found / Moved** |
| Transcript | **SMS 1 day before** · visits **09:00–18:00** · **Moved** → record new address |

- **Nothing** is built for international addresses (the transcript puts them out of scope)
- **Details I could not determine** asks whether a **neighbour can confirm**. It never asks about anything stated above

### 1.3 Build the same ticket again
**Ticket:** the ticket from 1.2 · **Comment:** `@Aetherion build` · **Wait:** 1 min
**Expect:** `BGV-… was already built — nothing created again`, listing the existing keys. No new tickets.

### 1.4 One capability → the ticket becomes a Story
**Ticket:** new **Task**, Summary `Address proof`
**Description:**
```
Candidate uploads one address proof: an electricity bill or a rental agreement, PDF or JPG, max 5 MB.
The officer marks it Accepted or Rejected, and a rejection needs a reason.
```
**Comment:** `@Aetherion build` · **Wait:** 2–3 min
**Expect:** `BGV-… is now a Story (it was a Task)`. Sub-tasks sit under it, and no new Story or Epic is created. The reply mentions 5 MB, Accepted/Rejected and the rejection reason.

### 1.5 The same work again → already exists
**Ticket:** new Story, Summary `Upload address proof`, **same description as 1.4**
**Comment:** `@Aetherion build` · **Wait:** 2–3 min
**Expect:** `This work already exists`, naming the 1.4 ticket. Nothing is created, **this ticket is unchanged**, and one email arrives.

### 1.6 A technical job → Task
**Ticket:** new Story, Summary `Maps API key`
**Description:** `Rotate the maps API key used for address lookup every 90 days: create a new key, update the configuration, then delete the old key.`
**Comment:** `@Aetherion build` · **Wait:** 2–3 min
**Expect:** `BGV-… is now a Task`. Sub-tasks cover the real steps only, and there is no "As a system" user story.

### 1.7 A production problem → Bug, priority Highest
**Ticket:** new Task, Summary `Candidate list broken`
**Description:** `Since yesterday's release, in production recruiters cannot open the candidate list. The page shows error 500. All clients are affected.`
**Comment:** `@Aetherion build` · **Wait:** 2–3 min
**Expect:** `BGV-… is now a Bug` with priority **Highest**. The description has What happens now, What should happen, Impact and Fixed when. **No children** are created.

### 1.8 A feature is not a Bug
**Ticket:** new Task, Summary `Address report button`
**Description:** `Add a button on the case page to download the address check result as a PDF.`
**Comment:** `@Aetherion build` · **Wait:** 2–3 min
**Expect:** it becomes a **Story**, not a Bug.

### 1.9 A vague request
**Ticket:** new Story, Summary `Faster address checks`, description **empty**
**Comment:** `@Aetherion build make address checks faster` · **Wait:** 1 min
**Expect:** `More information needed` with questions about address checks. Nothing is created, the ticket is unchanged, and one email arrives.

### 1.10 Review a Story the agent made
**Ticket:** one Story under the 1.2 Epic · **Comment:** `@Aetherion review` · **Wait:** 1–2 min
**Expect:** readiness **4/5 or higher**. There are no questions about facts it already has, and nothing about APIs, databases or SMS gateways.

### 1.11 Review a Sub-task
**Ticket:** one Sub-task of that Story · **Comment:** `@Aetherion review` · **Wait:** 1–2 min
**Expect:** no "has no acceptance criteria", and no repeat of the Story's questions.

### 1.12 Review a poor ticket
**Ticket:** new Story, Summary `Address check`, Description `Address checks should be quick and accurate.`
**Comment:** `@Aetherion review` · **Wait:** 1–2 min
**Expect:** low readiness, with findings that ask what "quick" and "accurate" mean and who does what. Nothing is created.

### 1.13 Explain
**Ticket:** the 1.2 Epic · **Comment:** `@Aetherion explain this ticket in simple words` · **Wait:** 1 min
**Expect:** `About BGV-…`, a plain explanation. Nothing is created.

### 1.14 Own request on an unrelated ticket → new ticket, no link
**Ticket:** BGV-1 (`Agent test ticket`)
**Comment:** `@Aetherion build let recruiters export the list of address checks to Excel with candidate name, address and result` · **Wait:** 2–3 min
**Expect:** a **new** Story with Sub-tasks is created. BGV-1 is **not** changed and has **no link**. The new Story ends with `Requested in a comment on BGV-1 by <you>, <date>.`

### 1.15 Own request on a related ticket → linked
**Ticket:** new Story, Summary `Address check reports`, Description `Recruiters need address check results for audits.`
**Comment:** `@Aetherion build send recruiters a weekly email listing address checks that are still open` · **Wait:** 2–3 min
**Expect:** a new Story is created and **relates to** your ticket. Your ticket is **not** converted.

### 1.16 Reply thread, no mention, chatter
- **Reply** (the Reply link) to any agent reply with `@Aetherion help` → the answer arrives **in that same thread**
- New comment `Looks good to me` → **no reply**
- New comment `@Aetherion what is the weather today?` → `Invalid request`, no email

### 1.17 A Sub-task cannot hold tickets
**Ticket:** any Sub-task from 1.2 · **Comment:** `@Aetherion build` · **Wait:** 1 min
**Expect:** `Nothing created — a Sub-task cannot hold other tickets`, naming its parent. The Sub-task is unchanged.

---

# SET 2 — Demo · Education verification

### 2.1 Help
**Ticket:** BGV-1 · **Comment:** `@Aetherion help`
**Expect:** the list of what it can do.

### 2.2 Build from four sources → Epic
**Ticket:** new Story, Summary `Education verification`
**Description:**
```
Verify each candidate's education degree.
- The recruiter sees the education check status on the case
- The policy is here: https://jiraagentdemo.atlassian.net/wiki/spaces/PR/pages/5341185
- More rules are in the attached requirements and the meeting transcript.
```
**Attach:** `Education_Verification_Requirements.docx` and `Education_Meeting_Transcript.txt`
**Comment:** `@Aetherion build` · **Wait:** 3–4 min
**Expect:** `BGV-… is now an Epic`, with Stories and Sub-tasks under it and no other Epic. These facts appear:

| From | Facts |
|---|---|
| Description | recruiter sees the status on the case |
| Confluence | **highest degree** only · copies deleted **90 days** after closing · registrar email from the **official domain** |
| Word file | PDF/JPG max **8 MB** · registrar **email** · **Confirmed / Not Found / Mismatch** · reminder after **12 working days** · **Unverifiable** after **20** · Mismatch → **amber flag** · **Blocked Institutions** never contacted |
| Transcript | candidate **emailed when closed** · officer adds a **note** to each result |

Nothing is built for certificates sent by post. It **asks** whether foreign universities are supported.

### 2.3 Explain
**Ticket:** the 2.2 Epic · **Comment:** `@Aetherion explain this ticket in simple words`
**Expect:** `About BGV-…`, a short explanation.

### 2.4 Review a Story it made
**Ticket:** one Story under the 2.2 Epic · **Comment:** `@Aetherion review`
**Expect:** readiness 4/5 or higher, and no invented topics.

### 2.5 One capability → Story
**Ticket:** new **Task**, Summary `Education check on hold`
**Description:**
```
A recruiter can put an education check On hold, with a reason.
While On hold, no registrar reminders are sent.
The recruiter can resume the check at any time.
```
**Comment:** `@Aetherion build`
**Expect:** `BGV-… is now a Story (it was a Task)`, with Sub-tasks under it.

### 2.6 A production problem → Bug, priority High
**Ticket:** new Task, Summary `Google login failing`
**Description:** `In production, some recruiters cannot log in with Google since this morning.`
**Comment:** `@Aetherion build`
**Expect:** `BGV-… is now a Bug` with priority **High** (only some users are affected), and no children.

### 2.7 Build again
**Ticket:** the 2.2 Epic · **Comment:** `@Aetherion build`
**Expect:** `already built — nothing created again`.

### 2.8 Already exists
**Ticket:** new Story, Summary `Hold an education check`, **same description as 2.5**
**Comment:** `@Aetherion build`
**Expect:** `This work already exists` (the 2.5 ticket). This ticket is unchanged.

### 2.9 Vague
**Ticket:** new Story, Summary `Better education checks`, description empty
**Comment:** `@Aetherion build improve education checks`
**Expect:** questions, and nothing created.

### 2.10 Own request on an unrelated ticket
**Ticket:** BGV-1 · **Comment:** `@Aetherion build let candidates see the status of their education check in the candidate portal`
**Expect:** a new Story. BGV-1 is unchanged and not linked, and the Story ends with `Requested in a comment on BGV-1 by …`.

---

# SET 3 — Main demo · Police clearance check

*Say* lines are optional talking points for the audience.

### 3.1 Help
**Ticket:** BGV-1 · **Comment:** `@Aetherion help`
*Say:* "One mention, and the router decides which agent answers."

### 3.2 Build from four sources → Epic
**Ticket:** new Story, Summary `Police clearance check`
**Description:**
```
Check whether a candidate has a police record before hiring.
- Candidate gives full name, date of birth and addresses for the last 5 years
- The recruiter sees the police check result on the case
- The policy is here: https://jiraagentdemo.atlassian.net/wiki/spaces/PR/pages/5373953
- More rules are in the attached spec and the meeting transcript.
```
**Attach:** `Police_Check_Spec.docx` and `Police_Check_Meeting_Transcript.txt`
**Comment:** `@Aetherion build` · **Wait:** 3–4 min
*Say:* "It read the ticket, a Word file, a meeting transcript and a Confluence page, and turned this ticket itself into the Epic. It didn't create a copy of it."
**Expect:** `BGV-… is now an Epic`, with Stories and Sub-tasks under it and no other Epic. These facts appear:

| From | Facts |
|---|---|
| Description | full name, date of birth, **last 5 years** of addresses · recruiter sees the result |
| Confluence | result valid **6 months** · record details **never in the candidate portal** · minor **traffic offences not reported** |
| Word file | **police verification portal** · **Clear / Record Found / Pending** · Pending over **15 working days** → escalate · Record Found → **red flag** + **compliance review** |
| Transcript | **signed authorisation letter** first · re-send **at most once** · result seen only by **compliance and recruiter** |

Nothing is built for checks outside India. It **asks** whether the candidate is told the result.

### 3.3 Show what changed on the ticket
Open the 3.2 ticket. *Say:* "Same key, same comments and attachments. The description is structured, and the original is kept at the bottom under Original request."

### 3.4 Review a Story it made
**Ticket:** one Story under the 3.2 Epic · **Comment:** `@Aetherion review`
*Say:* "A second agent checks the first one's work and only raises what the sources never said."
**Expect:** readiness 4/5 or higher.

### 3.5 Explain
**Ticket:** the 3.2 Epic · **Comment:** `@Aetherion explain this ticket in simple words`

### 3.6 One capability → Story
**Ticket:** new **Task**, Summary `Change phone number`
**Description:**
```
A candidate can change their mobile number on their profile.
A 6-digit code is sent to the new number and must be entered within 10 minutes.
```
**Comment:** `@Aetherion build`
**Expect:** `BGV-… is now a Story`, with Sub-tasks under it. 6 digits and 10 minutes are carried.

### 3.7 A technical job → Task
**Ticket:** new Story, Summary `Portal timeout`
**Description:** `Change the police portal timeout from 30 to 60 seconds in the configuration, then restart the service.`
**Comment:** `@Aetherion build`
**Expect:** `BGV-… is now a Task`.

### 3.8 A production problem → Bug, Highest
**Ticket:** new Task, Summary `Case search broken`
**Description:** `Since the release, in production case search returns no results for any candidate. All clients are affected.`
**Comment:** `@Aetherion build`
*Say:* "A production outage becomes a Bug with the highest priority, not a feature."
**Expect:** `BGV-… is now a Bug` with priority **Highest**, and no children.

### 3.9 Build again
**Ticket:** the 3.2 Epic · **Comment:** `@Aetherion build`
**Expect:** `already built — nothing created again`.

### 3.10 Already exists
**Ticket:** new Story, Summary `Update mobile number`, **same description as 3.6**
**Comment:** `@Aetherion build`
**Expect:** `This work already exists`. Nothing is created, and this ticket is unchanged.

### 3.11 Vague
**Ticket:** new Story, Summary `Faster police checks`, description empty
**Comment:** `@Aetherion build make police checks faster`
**Expect:** questions. Nothing is created.

### 3.12 Own request on an unrelated ticket
**Ticket:** BGV-1 · **Comment:** `@Aetherion build let compliance export all Record Found results for the month to Excel`
**Expect:** a new Story. BGV-1 is unchanged and not linked, and the Story ends with `Requested in a comment on BGV-1 by …`.

### 3.13 Own request on a related ticket
**Ticket:** new Story, Summary `Police check notifications`, Description `Recruiters need to know when police checks finish.`
**Comment:** `@Aetherion build email the recruiter when a police check result is Clear or Record Found`
**Expect:** a new Story that **relates to** your ticket.

### 3.14 Reply thread
**Reply** to the agent's reply from 3.8 with `@Aetherion help`
**Expect:** the answer arrives in the same thread.

### 3.15 A Sub-task cannot hold tickets
**Ticket:** any Sub-task from 3.2 · **Comment:** `@Aetherion build`
**Expect:** refused, naming its parent. Nothing is changed.

---

# Results

| Step | Pass / Fail | Ticket key | Notes |
|---|---|---|---|
| 1.1 | | | |
| 1.2 | | | |
| 1.3 | | | |
| 1.4 | | | |
| 1.5 | | | |
| 1.6 | | | |
| 1.7 | | | |
| 1.8 | | | |
| 1.9 | | | |
| 1.10 | | | |
| 1.11 | | | |
| 1.12 | | | |
| 1.13 | | | |
| 1.14 | | | |
| 1.15 | | | |
| 1.16 | | | |
| 1.17 | | | |
| 2.1–2.10 | | | |
| 3.1–3.15 | | | |

**If a step fails:** send the ticket key, the reply (or "no reply") and the time you commented.
