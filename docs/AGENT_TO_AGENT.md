# One agent calling another

What the Aetherion platform actually supports, verified against the installed
SDK rather than assumed. Everything here was checked by reading
`aetherion_sdk` and by running `docs/a2a_proof_of_concept.py`; anything that
could not be checked locally is listed under
[What I could not confirm](#what-i-could-not-confirm).

---

## a. The mechanism

**A child agent is a Temporal child workflow.** The SDK exposes exactly one way
to start one:

```python
from aetherion_sdk import agentExecutor

result = await agentExecutor.execute(
    "JiraRequirementReview",      # the REGISTERED agent name
    {"issue_key": "FL-130", "mode": "delegated", "run_id": run_id},
    execution_timeout=timedelta(minutes=10),
    retry_policy=RetryPolicy(maximum_attempts=1),
    workflow_id=f"review-{run_id}",
)
```

`agentExecutor.execute(workflow_type, *args, **temporal_options)` accepts only
these six options, and rejects anything else:

```
execution_timeout   retry_policy   run_timeout
task_queue          task_timeout   workflow_id
```

**The name is the registered agent name, not the class or the folder.** For us:
`JiraTaskCreation` and `JiraRequirementReview`. This is why those names were
not changed when the folders were renamed — the router dispatches on them.

### Is there more than one way? Yes — three, and we use the first

| Call | Shape | When it fits |
|---|---|---|
| `agentExecutor.execute(...)` | one child, awaited | **our case** — one agent per comment |
| `agentExecutor.gather(*tasks)` | several awaitables concurrently | independent children |
| `agentExecutor.run_parallel({label: (name, payload)})` | fan out, returns a dict keyed by label | independent children, named results |

The BOTH route ("check it's clear, and if it is, break it down") is **conditional**
— the review's verdict decides whether the build runs at all. So it is two
sequential `execute` calls, not `run_parallel`. Parallelism would run the build
before knowing whether it should.

### Where a child runs

Task queue resolution, read from the SDK:

- default queue: **`aetherion-workflows`** (confirmed by calling
  `_resolve_default_task_queue()`)
- `AETHERION_AGENT_TASK_QUEUE` overrides the default
- `AETHERION_AGENT_TASK_QUEUE_MAP` maps individual agents to their own queues;
  empty unless configured

So children can live on separate workers. Nothing needs to change in our code
for that — it is deployment configuration.

---

## b. What crosses the boundary, and who serialises

**Temporal serialises. You never do.** The payload is converted to
`json/plain`, then passed through `SecurePayloadCodec`, which adds **AES-GCM
encryption** and **offloads oversized payloads to Redis** with a TTL. A large
result is therefore slow, not fatal.

**The result contract survives intact — verified, not assumed.** Encoding a real
`AgentResult` through the same converter the platform uses and decoding it back:

```
encoding   : json/plain
size       : 738 bytes
identical  : True
outcome    : <Outcome.CREATED: 'CREATED'>   (a real enum, not a string)
missing[0] : spec_matrix.xlsx (password-protected) — please re-attach it unprotected
```

**One thing must change, and it is subtle.** The child must return
`result.to_dict()`, and the router must call `AgentResult.from_dict(payload)`.

Returning the dataclass *appears* to work — it encodes without complaint — but
decodes on the far side as a plain `dict`, with `outcome` as the string
`'CREATED'` rather than `Outcome.CREATED`. Nothing raises. The router would
simply compose the wrong reply.

Both agents already do the right thing:
`jira-task-creation` returns `to_contract(...).to_dict()`;
`jira-requirement-review` returns `AgentResult(...).to_dict()`.

---

## c. Sync or async — your position, confirmed

**You said: the router calls children synchronously and owns any async decision
itself. That is correct, and the platform supports it directly.**

`execute` is `async def` and returns the child's result, so the parent awaits
completion. That is synchronous *from the router's point of view* while
remaining durable underneath: if the worker restarts mid-call, Temporal replays
the router's history and the child keeps running.

**Why it has to be this way.** The router composes one reply from the child's
result. If dispatch were fire-and-forget, the router would finish with nothing
to say, and something else would have to post later — which is precisely the
"two writers" problem the whole design removes. **Whoever waits for the result
must be whoever posts the reply.**

**About the Execution Mode toggle on the review agent's trigger form.** That is
the *manual* entry point's setting, for a person launching a batch from the UI
and not wanting to sit and watch it. It is unrelated to the routed path: the
router always calls with `mode="delegated"` and always awaits. A long review
should be bounded with `execution_timeout`, not made asynchronous.

The cost is a bounded wait. Jira does not care — the webhook returns immediately
and the reply arrives as a comment whenever it is ready.

---

## d. Failure modes

Every one of these presents to the router as an exception from `await execute`.
The router catches `TemporalError` and **still posts a reply** — it owns the
reply, so a dead child cannot mean silence.

| What happened | What the router sees | What it does | What the user sees |
|---|---|---|---|
| Child raised | `ChildWorkflowError` wrapping the cause | compose a FAILED result, no child named | "Could not complete this request" + what was safe to retry |
| Child exceeded its timeout | `TimeoutError` | same, reason "timed out" | same, and says a retry is safe if nothing was created |
| Child not deployed / name wrong | `ApplicationError` (workflow type not registered) | same, reason "agent not available" | "…did not respond. Nothing was created, so asking again is safe." |
| Queue full / no worker polling | the call blocks, then `TimeoutError` | same as timeout | same |
| Child ran but returned a malformed payload | `ValueError` from `from_dict` | FAILED, `errors` names the contract violation | as above |
| Router itself crashed mid-run | Temporal replays it | idempotency key means the child is not re-run | one reply, eventually |

**"Handled by" names no child on any of these.** The router knows the child was
*chosen*; it must not claim it *ran*. Proven in the proof-of-concept:

```
ok             -> CREATED  | handled by: Work Breakdown
crash          -> FAILED   | handled by: (no child named)
timeout        -> FAILED   | handled by: (no child named)
not_deployed   -> FAILED   | handled by: (no child named)
```

The distinction that matters to a user is **"was anything created?"**, because
that decides whether retrying is safe. A timeout is the dangerous case: the
child may have created issues before dying. The router must say "nothing was
created" only when it knows that — otherwise it says the work may be partially
done and names the idempotency key, which makes a retry safe anyway.

---

## e. Where deterministic-workflow rules bite

**This is the sharpest constraint on the router, and it has already broken this
codebase once.** From the existing regression test:

```
RestrictedWorkflowAccessError: Cannot access os.environ.get from inside a
workflow.
  agent.py:428   is_question_comment(trigger_body)
  ingest.py:405    strip_trigger_mentions(body, trigger_keyword())
  keywords.py:39     os.environ.get("LTW_TRIGGER_KEYWORD", ...)
```

An agent function **is** the workflow. Inside it you may not:

- read `os.environ`
- read files
- call the network
- use `random`, `uuid4`, or wall-clock time
- do anything else whose result could differ on replay

All of that belongs in a `@tool` activity, and the answer is handed back as data.

**What this means for JiraOrchestration specifically:**

| The router needs | Where it must happen |
|---|---|
| the trigger keyword, thresholds, the routing table | a `@tool` — they come from the environment |
| the mention filter and self-guard | pure functions over data the tool returned — fine in the workflow |
| the idempotency check and the per-issue lock | a `@tool` — they touch shared storage |
| minting the `run_id` | a `@tool` — `uuid4()` is non-deterministic |
| the classifier's model call | a `@tool` — network |
| dispatching to the child | the workflow — `agentExecutor.execute` is designed for it |
| composing the reply from the contract | pure — fine in the workflow |
| posting the reply, labels, email | a `@tool` — network |

**A bug this review caught.** Phase 2 gave both agents a version lookup that read
`metadata.json` at call time — file I/O inside the workflow. The existing
sandbox tests only checked `os.environ`, so it passed. Both now carry
`AGENT_VERSION` as a constant, kept in step by `scripts/publish.py` and by
`tests/test_version_agreement.py`, and a new test refuses any file read from a
workflow module.

---

## f. Identity — whose credentials act

**Unchanged by routing, because nothing about credentials travels with the
call.** Each agent reads `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` from
its own `.env`, which ships inside its own deploy artifact.

| Action | Whose account |
|---|---|
| Creating the Epic/Stories/Sub-tasks | the **task-creation** agent's Jira account — it does the creating |
| Reading the issue, attachments, Confluence | whichever agent is reading, using its own token |
| Posting the single reply comment | the **router's** Jira account |
| Stamping the label, the answered-comment property | the **router's** account |

So "Created by" on a Jira issue is the child's service account, and the reply
comment's author is the router's. **They can be the same account**, and today
they would be — all three read the same variables.

Two consequences worth stating:

- **The self-guard cannot rely on identity.** The agent usually runs as the same
  Jira account as the people using it, so "who wrote this comment" does not
  distinguish the system from a human. That is why `is_agent_comment` matches
  the `AetherionAgent` signature instead, and why the signature is deliberately
  spelled so the mention filter cannot match it.
- **A project the child may not write to fails in the child**, not the router.
  It comes back as a FAILED contract with the Jira error in `errors`, and the
  router reports it.

---

## g. Idempotency across the chain

Three independent layers, and they compose:

```
1. ROUTER INGRESS   key = comment.id + webhookEvent, with a TTL
                    A redelivered comment never reaches classification.
                    Log line carries the ORIGINAL run_id.

2. CHILD WORKFLOW   workflow_id = f"{agent}-{run_id}"
   ID               Temporal refuses a duplicate workflow id, so a router
                    replay cannot start the child twice.

3. CHILD'S OWN      the idempotency label derived from the requirement text
   LABEL            + project. A retry finds the label and returns the
                    existing issues instead of creating duplicates.
```

**The case that matters: the router is redelivered after the child already
ran.** Layer 1 stops it. If it somehow did not, layer 2 refuses the duplicate
workflow id. If *that* did not, layer 3 means the child returns the existing
hierarchy rather than a second one. Nothing creates duplicate Jira issues
without all three failing.

`workflow_id` is one of the six accepted options, so the router sets it
explicitly — it must be derived from the `run_id`, never from a timestamp or a
random value.

---

## h. Observability — one comment, traced end to end

The `run_id` is minted once at ingress and carried everywhere:

```
run 4a81f7c2  webhook received, comment 10501 on FL-120
run 4a81f7c2  classified BUILD (Layer 1, verb "build")
run 4a81f7c2  dispatching JiraTaskCreation, workflow_id=build-4a81f7c2
run 4a81f7c2    [child] created FL-121, FL-122, FL-123, FL-124
run 4a81f7c2  contract received: CREATED, 4 issues, email recommended
run 4a81f7c2  posted comment 10502, labels [ltw-processed]
run 4a81f7c2  emailed kothakota.sailaxmanrao@calfus.com
```

The same id appears in the Jira comment ("run 4a81f7c2"), in the email, and in
every log line on both sides — so one comment a user points at can be traced to
the child that answered it.

Temporal adds its own: the child's `workflow_id` is derived from the `run_id`,
so a run is findable in the Temporal UI from the Jira comment alone.

---

## A full request, start to finish

```
Jira            JiraOrchestration (workflow)        Child agent            Jira
 │                     │                       (child workflow)        │
 │ comment_created     │                             │                 │
 ├────────────────────►│                             │                 │
 │                     │ @tool ingress_check         │                 │
 │                     │  ├ mention filter           │                 │
 │                     │  ├ self-guard (signature)   │                 │
 │                     │  ├ idempotency (id+event)   │                 │
 │                     │  ├ per-issue lock           │                 │
 │                     │  └ mint run_id ─────────────┼──► 4a81f7c2     │
 │                     │                             │                 │
 │                     │ classify (pure, Layer 1)    │                 │
 │                     │   └ no verb? @tool model    │                 │
 │                     │      call (Layer 2)         │                 │
 │                     │                             │                 │
 │                     │ agentExecutor.execute(      │                 │
 │                     │   "JiraTaskCreation",       │                 │
 │                     │   {mode: delegated,         │                 │
 │                     │    run_id: 4a81f7c2},       │                 │
 │                     │   workflow_id=              │                 │
 │                     │     "build-4a81f7c2")       │                 │
 │                     ├────────────────────────────►│                 │
 │                     │                             │ reads the issue │
 │                     │                             ├────────────────►│
 │                     │                             │ creates issues  │
 │                     │                             ├────────────────►│
 │                     │                             │ posts NOTHING   │
 │                     │◄────────────────────────────┤                 │
 │                     │   AgentResult.to_dict()     │                 │
 │                     │   (json/plain, encrypted)   │                 │
 │                     │                             │                 │
 │                     │ compose ONE reply           │                 │
 │                     │  (outcome → layout table,   │                 │
 │                     │   never on produced_by)     │                 │
 │                     │                             │                 │
 │                     │ @tool post_reply ───────────┼────────────────►│
 │                     │ @tool stamp_label ──────────┼────────────────►│
 │                     │ @tool send_email (≤1)       │                 │
 │◄────────────────────┤                             │                 │
 │  one comment,       │                             │                 │
 │  signed             │                             │                 │
```

---

## What I could not confirm

Stated plainly rather than guessed. Each needs a live worker, which is Phase 6.

1. ~~**That a deployed child is reachable by name from another deployed agent.**~~
   **RESOLVED — observed working.** Live dispatches from the router to both
   children succeed (e.g. BGV-57: Work Breakdown created an Epic, 4 Stories and
   9 Sub-tasks; reviews on BGV-1). It needed one thing this document did not
   foresee: an explicit `task_queue` per child, because each deployed agent
   polls its own `<agent_id>-task-queue`, and a child started without one lands
   on the router's queue, where no worker knows its type (FLAWS.md F22).
2. **The payload size at which Redis offload kicks in**, and its TTL default.
   The mechanism is confirmed (`SecurePayloadCodec`, `DEFAULT_OFFLOAD_TTL_DAYS`);
   the numbers are not. Our contracts are ~700 bytes, so this is unlikely to
   matter, but a review with fifty findings is untested.
3. **Whether `AETHERION_AGENT_TASK_QUEUE_MAP` is set in your deployment.** It
   reads empty locally. If children run on a different queue than the router
   expects, dispatch will time out rather than fail fast — the symptom would look
   like "child not deployed".
4. **What the platform does with a child workflow's retry policy by default.**
   I would set `maximum_attempts=1` explicitly on the build route, because a
   retried creation is exactly what the idempotency label exists to survive — but
   I have not confirmed the platform's default.
5. **Whether `RestrictedWorkflowAccessError` covers file reads** as well as
   `os.environ`. I assumed it does and removed the file read regardless, because
   re-reading a file on every run is wrong either way. Worth confirming.

## If the platform does not support something assumed here

The one genuine dependency is `agentExecutor.execute` reaching a separately
deployed agent. If that turns out not to work across deployments, the nearest
supported thing is to **merge the router into one of the agents as an entry
mode** — the same classification and composition code, dispatching to in-process
functions rather than child workflows. The result contract and the composer stay
exactly as they are; only the dispatch step changes. That would cost the
independent deployability of the children, not the architecture.


---

## The shared rubric (D1, 2026-09-25)

`shared/rubric.py` is the one definition of a good ticket. Work Breakdown
checks its own draft against it before any Jira write; Requirement Review takes
its nine finding categories from it. Two definitions drift; one cannot.

| Check | Kind | Meaning |
|---|---|---|
| UNTESTABLE_CRITERION | AVOIDABLE | an acceptance or completion criterion nobody can check |
| STATED_DETAIL_MISSING | AVOIDABLE | a detail the input states is absent from every ticket |
| NO_ACTOR | AVOIDABLE | a Story naming no user or role ("As a user…") |
| STATED_EDGE_CASE_MISSING | AVOIDABLE | an edge case the input describes has no criterion |
| UNTRACED_SUBTASK | AVOIDABLE | a Sub-task that delivers no acceptance criterion |
| BOILERPLATE | AVOIDABLE | text that fits any ticket (`BOILERPLATE_PHRASES`) |
| ASKED_WHAT_WAS_STATED | AVOIDABLE | an open question the input already answers |
| ABSENT_EVERYWHERE | UNAVOIDABLE | a decision no source makes — asked, never invented |

**Work Breakdown (bar 1):** draft → score (code + one model call; every model
finding must quote the input or it is discarded) → AVOIDABLE findings get ONE
regeneration → questions the input answers are removed → what remains is named
in the reply. Target: zero avoidable findings.

**Requirement Review (bar 2):** every finding must cite a source label that was
actually read; one that does not is dropped and counted as `over_reach` — the
review's own metric, never charged to the ticket. Gaps already listed under
"Open questions — not stated in the requirement" are not raised again.
