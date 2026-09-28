# Jira Orchestration

One ingress for Jira comment webhooks. It decides which agent should answer a
comment, calls exactly one of them, and posts exactly one reply.

It **routes**. It never gathers deep context and never creates Jira issues —
those belong to the agents it calls.

---

## Why this exists

Jira webhooks are registered per project and event; they are not addressed to a
consumer. Point three agents at the same event and **every comment wakes all
three**, each decides "is this mine?" independently, two can say yes, and you
get two replies, two label stamps and possibly duplicate issues with no shared
lock.

So one webhook points here, and this owns everything that must happen exactly
once:

| Step | Why here and not in an agent |
|---|---|
| Mention filter | one place decides what counts as a request |
| Self-guard | the system must never answer itself |
| Idempotency (`comment.id` + event, with a TTL) | Jira redelivers; the label is the *second* line of defence |
| Classification | one decision, recorded, so routing is debuggable |
| **The single reply** | see [ARCHITECTURE.md](ARCHITECTURE.md) |

---

## What it does with a comment

```
mention filter → self-guard → idempotency → lock → classify → dispatch
   → compose ONE reply → post, label, at most one email
```

Classification is three layers, in order:

| Layer | What | Cost |
|---|---|---|
| **1** | an explicit verb — `build`, `review`, `explain`, `help` | free, deterministic, auditable |
| **2** | one classifier call over a closed set: `BUILD / REVIEW / QUESTION / CHATTER` | one cheap model call |
| **3** | **ask** — "did you mean build or review?" | one comment |

Layer 3 is the design, not a failure. One round-trip costs a comment; a wrong
BUILD costs ten stories somebody has to find and delete.

**Thresholds are asymmetric.** Work breakdown needs 0.80 because it writes to
Jira. Review needs 0.60 — its only write is a comment, and over this path it is
forced draft-only.

---

## Prerequisites

- Python **3.12** (the SDK ships a cp312 wheel only — 3.13 will not resolve)
- [`uv`](https://docs.astral.sh/uv/)
- A Jira Cloud account with permission to comment on the target project
- The two child agents published and reachable — see
  [Troubleshooting](#troubleshooting) if dispatch times out

---

## Setup

```bash
cd jira-orchestration
uv sync
cp .env.template .env      # then fill it in
```

### Every environment variable

| Variable | Required | Default | What it does |
|---|---|---|---|
| `JIRA_BASE_URL` | yes | — | e.g. `https://acme.atlassian.net` |
| `JIRA_EMAIL` | yes | — | the account that posts the reply |
| `JIRA_API_TOKEN` | yes | — | that account's API token |
| `ORCH_TRIGGER_KEYWORD` | no | `Aetherion` | the word that makes it act |
| `ORCH_PROCESSED_LABEL` | no | `aetherion-processed` | stamped once a comment is answered |
| `ORCH_IDEMPOTENCY_TTL_HOURS` | no | `24` | how long a handled `comment.id` is remembered |
| `ORCH_CLASSIFIER_MODEL` | no | `gpt-5.1` | the Layer 2 model |
| `ORCH_NOTIFY_EMAILS` | no | — | who hears about failures |
| `ORCH_ALLOWED_PROJECT_KEYS` | no | *(any)* | restricts which projects it will answer in |

> `.env` **ships inside the deploy artifact.** Whatever is in it when you
> publish is what production uses. `scripts/publish.py` prints what would ship,
> with secrets masked, and refuses on anything that looks like local testing.

---

## Running the tests

```bash
uv run pytest -q
uv run ruff check src tests
uv run black --check src tests
```

The classification and composition logic is pure, so most of the suite runs with
no Jira, no model and no worker.

---

## Deploying

```bash
uv run python scripts/publish.py --bump patch
```

Seven gates, in order: shared mirror in sync → tests → lint → all three version
locations agree → version raised → preflight → publish. `--dry-run` runs every
check without uploading.

**Publish the children before the router.** A router dispatching to an agent
that is not live fails in a way users see; children published early are simply
not yet routed to.

---

## Troubleshooting

| What you see | What it usually means |
|---|---|
| No reply at all | The comment did not contain the trigger keyword, or the webhook is not firing |
| Every comment answered twice | Two webhooks are registered. Only this agent should receive `comment_created` |
| "…did not respond" in the reply | The child agent is not deployed, or is on a task queue this router cannot reach — check `AETHERION_AGENT_TASK_QUEUE_MAP` |
| The system replying to itself | The footer matched the mention filter. It is spelled `AetherionAgent` precisely so it cannot; a test asserts this |
| `RestrictedWorkflowAccessError` | Something in the workflow read the environment, a file, or the clock. It belongs in a `@tool` |
| Routing looks wrong | Read the **Routed to** line in the reply — it names the layer and the confidence that decided |

**The reply comment is always the first place to look.** It says what ran, why it
was chosen, what was read, and what was missing.

---

## Further reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — how it works inside, and why the router
  owns the reply
- [BEHAVIOUR.md](BEHAVIOUR.md) — exactly what it replies, word for word
- [../docs/AGENT_TO_AGENT.md](../docs/AGENT_TO_AGENT.md) — how one agent calls
  another on this platform
- [../FLAWS.md](../FLAWS.md) — every defect found so far, and its status
