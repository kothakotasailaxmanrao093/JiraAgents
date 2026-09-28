# How the router works, inside

This is the router's WORKING.md — the end-to-end flow, every decision branch,
and what each module does.

---

## The flow, once

```
Jira comment_created
        │
        ▼
┌───────────────────────────────────────────────┐
│ @tool ingress_check          (ACTIVITY)       │
│  1 mention filter    no keyword → drop silent │
│  2 self-guard        our signature → drop     │
│  3 idempotency       seen before → drop       │
│  4 mint run_id       uuid4 is non-deterministic│
└───────────────────────────────────────────────┘
        │ proceed
        ▼
┌───────────────────────────────────────────────┐
│ classify_by_verb             (PURE, workflow) │
│   explicit verb → route, no model call        │
└───────────────────────────────────────────────┘
        │ no verb
        ▼
┌───────────────────────────────────────────────┐
│ @tool classify_intent        (ACTIVITY)       │
│   one call, closed set, returns scores        │
└───────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────┐
│ classify_by_model            (PURE, workflow) │
│   above threshold → route                     │
│   below           → ASK (Layer 3)             │
└───────────────────────────────────────────────┘
        │
   ┌────┴────────────────────┐
   ▼                         ▼
dispatch one child      answer directly
(child workflow,        (help / question /
 awaited)                chatter / ambiguous)
   │                         │
   └────────┬────────────────┘
            ▼
┌───────────────────────────────────────────────┐
│ compose                      (PURE, workflow) │
│   outcome → layout table, never produced_by   │
└───────────────────────────────────────────────┘
            ▼
┌───────────────────────────────────────────────┐
│ @tool post_reply             (ACTIVITY)       │
│   post comment → record answered → add label  │
│   in that order, and only that order          │
└───────────────────────────────────────────────┘
```

**Every path below the ingress gate posts exactly once.** There is no branch
that posts twice, and none that decides to answer and then stays silent.

---

## What each module does

| Module | Role | Pure? |
|---|---|---|
| `routing/catalog.py` | the agent table: names, verbs, thresholds, timeouts | yes |
| `routing/ingress.py` | gates 1–2 and the key shapes for 3–4 | yes |
| `routing/classify.py` | the three layers | yes |
| `routing/compose.py` | the envelope and the outcome→layout table | yes |
| `routing/settings.py` | every environment variable | **activity only** |
| `agent/router_flow.py` | the sequence | yes |
| `agent/router_agent.py` | the SDK binding | yes |
| `tools/tools.py` | Jira, the model, email | **activity only** |

The pure/activity split is enforced by `tests/test_workflow_sandbox.py`, which
fails if a workflow module can so much as *import* something that reads the
environment — or calls `uuid4()`, or reads the clock, or opens a file.

---

## Why the router owns the reply

The alternative is each agent posting and signing its own. It was rejected:

- exactly-once stops being structural and becomes a promise three services each
  have to keep;
- a route that runs both children produces two comments the user must reconcile;
- formatting drifts as each agent grows its own comment builder;
- a half-failed child leaves either no comment or a misleading one;
- a fourth agent means a fourth comment writer to keep in step.

**Attribution is a content concern, not a transport concern.** The byline costs
one line inside the comment. It is not worth three writers.

Three things stay separate and are never conflated:

1. **Who posts** — always this router. One writer, one account.
2. **Who authored** — named *in* the comment, from the returned contract.
3. **Who is accountable** — one signature, always identical.

---

## Adding a fourth agent

One entry in `routing/catalog.py`:

```python
AgentSpec(
    agent_name="SomeNewAgent",        # what the platform registered
    display_name="Some New Thing",    # what a person reads
    intent="ESTIMATE",
    verbs=("estimate", "size"),
    confidence=0.80,                  # high if it writes
    writes_to_jira=True,
    forced_payload={"mode": "delegated"},
)
```

…plus that agent returning `shared.contract.AgentResult`. **No router code
changes.** The composer is proven not to be an obstacle by
`test_the_composer_works_for_an_agent_it_has_never_seen`, which feeds it a
synthetic agent and checks the reply is still correct.

---

## Decision branches, in full

| Situation | Layer | Dispatches | Posts | Names a child |
|---|---|---|---|---|
| No trigger keyword | — | no | **no** | — |
| Our own reply | — | no | **no** | — |
| Already answered (redelivery) | — | no | **no** | — |
| Explicit verb `build`/`break down`/… | 1 | Work Breakdown | yes | yes |
| Explicit verb `review`/`assess`/`check`/… | 1 | Requirement Review | yes | yes |
| `help` | 1 | no | yes | **no** |
| `explain` / `what is this` | 1 | **Work Breakdown** (F25) | yes | yes |
| Classifier ≥ threshold | 2 | that agent | yes | yes |
| Classifier torn between BUILD and REVIEW (combined ≥ 0.75) | 2 | **Requirement Review** (F27) | yes | yes |
| Classifier says QUESTION | 2 | **Work Breakdown** (F25) | yes | yes |
| Classifier says CHATTER | 2 | no | yes | **no** |
| Classifier below threshold, genuinely unclear | 3 | no | yes | **no** |
| Classifier unreachable | 3 | no | yes | **no** |
| Child crashed / timed out / absent | — | attempted | yes | **no** |
| Child returned nonsense | — | attempted | yes | **no** |
| Reply could not be posted | — | — | tried | admins alerted |

The two rows that matter most are the last three: **a failure still produces a
reply**, and **a child that did not complete is never named**. The router knows
which agent it *chose*; claiming it *ran* is a lie the reader cannot check.

---

## Dispatch resilience

`agentExecutor.execute` is called with a bounded `RetryPolicy` (2s initial
interval, ×2 backoff, 10s cap, 3 attempts) rather than Temporal's own default of
unbounded automatic retry. **This bounds only a child that starts and then
fails** — a live run through F30 proved it does nothing for a child that never
starts at all because no worker ever polls its task queue (the F22
misconfigured-queue case): with no failure to retry, Temporal just waits out
the full `execution_timeout` before reporting a timeout. That case is bounded
by `timeout_minutes` on the catalog entry instead (5 for Work Breakdown, 4 for
Requirement Review — lowered from 15/10 by F30, so the wait is minutes, not a
quarter of an hour, while F22 remains unconfigured).

`non_retryable_error_types` could still distinguish "will never succeed" (bad
payload) from "might succeed on retry" (a worker mid-restart) for the
child-starts-then-fails case this policy *does* cover, but the platform's real
error-type strings for that were not confirmed this session.

Whatever the exception, its real type and message reach three places, never
flattened to a fixed string (F23):

1. an `logger.error` line carrying the `run_id`, for cross-referencing;
2. a **Problems** section in the comment (`compose.py::router_only`);
3. `notify_admins`, with a **Notification** line confirming whether the email
   actually sent — omitted, not claimed, when no admin address is configured.

---

## Health check

`JiraOrchestration` accepts a side-effect-free probe payload:

```bash
uv run aetherion agent JiraOrchestration '{"health_check": true}'
```

It dispatches a minimal payload to every agent in `CATALOG` with
`maximum_attempts=1` and a 30s timeout, and reports `{"reachable": bool, ...}`
per agent — nothing is posted to Jira, nothing is created, and ingress is
bypassed entirely (`agent/router_agent.py`, `run_health_check` in
`router_flow.py`).

This has to be a **workflow-side payload branch**, not a separate `@tool`:
starting a Temporal child workflow is only possible from workflow context, and
health-checking every configured child means starting (and awaiting) each of
them exactly as a real dispatch would.

---

## Ordering inside `post_reply`

Comment → record answered → label, in that order, in one activity.

They are one logical act ("this comment has been answered"), and splitting them
leaves states that break the system:

- **record before post**, and a failed post leaves the comment marked answered:
  silence the user cannot escape, because a retry is now suppressed.
- **label before post** has the same shape, and the label is what a human scans
  for.

Recording after the post means the worst case is answering *twice*, which is
visible and recoverable. That is the right way round.

---

## Idempotency, three layers deep

```
1. INGRESS      key = comment.id + webhookEvent, in a Jira issue property
                Checked BEFORE classification, so a redelivery costs neither
                a model call nor a child workflow.

2. CHILD        workflow_id = f"{intent}-{run_id}"
   WORKFLOW ID  Temporal refuses a duplicate id, so a router replay cannot
                start the child twice.

3. CHILD'S OWN  the work-breakdown agent's idempotency label, derived from
   LABEL        the requirement text. A retry finds it and returns the
                existing issues rather than creating more.
```

Nothing creates duplicate Jira issues without all three failing.

The event is part of the key because `comment_created` and `comment_updated`
carry the same comment id, and **editing a comment to add a new instruction is
genuinely a new request**.
