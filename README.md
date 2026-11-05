# Jira agents

Three Aetherion agents that answer `@Aetherion` comments on Jira tickets. One
webhook reaches all of them, and a comment is answered exactly once.

```
Jira comment_created
        │
        ▼
  jira-orchestration          the router — filters, de-duplicates,
  "Jira Orchestration"        classifies, dispatches, posts THE reply
        │
   ┌────┴─────────────────┬──────────────────┐
   ▼                      ▼                  ▼
jira-task-creation   jira-requirement-   answers directly
"Work Breakdown"     review              (help, questions,
creates Epics,       "Requirement        "which did you mean?")
Stories, Sub-tasks   Review"
                     finds gaps,
                     creates nothing
```

| | Folder | Registered as | Writes to Jira |
|---|---|---|---|
| Router | `jira-orchestration/` | `JiraOrchestration` | one comment, one label |
| Work breakdown | `jira-task-creation/` | `JiraTaskCreation` | Epics, Stories, Sub-tasks |
| Requirement review | `jira-requirement-review/` | `JiraRequirementReview` | **nothing**, by default |

The folder names and the registered agent names differ on purpose: the
registered name is what the Automation rule and the platform dispatch on, so
renaming one registers a *new* agent. See [FLAWS.md](FLAWS.md) O5.

---

## What happens to a comment

```
@Aetherion build let depot staff record a tyre pressure check
```

1. **Gate.** Does it mention the agent? Did we write it? Have we answered it
   already? Any "no" and nothing happens — silently.
2. **Classify.** An explicit verb (`build`, `review`, `explain`, `help`) routes
   for free. Otherwise one cheap model call over a closed set. Below the
   threshold, the router **asks** rather than guessing.
3. **Dispatch.** Exactly one agent, as a Temporal child workflow, awaited.
4. **Reply.** The router composes one comment from the contract the child
   returned, posts it, stamps the label, records the comment as answered.

Thresholds are asymmetric: **0.80 to create Jira issues, 0.60 to review.** Being
wrong costs ten stories in one case and one comment in the other.

---

## Where to look

| | |
|---|---|
| [WORKFLOW.md](WORKFLOW.md) | the commands, the full flow, running it locally, testing and publishing |
| `<agent>/README.md` | how to run and deploy that one |
| `<agent>/BEHAVIOUR.md` | exactly what it replies, word for word |
| `<agent>/ARCHITECTURE.md` | how it works inside |

---

## Running everything

Each agent is its own project with its own virtualenv.

```bash
cd jira-task-creation        && uv run pytest -q     # 930 passed, 1 skipped
cd ../jira-requirement-review && uv run pytest -q    # 219 passed
cd ../jira-orchestration     && uv run pytest -q     # 212 passed
```

> **A fresh `uv sync` currently fails** — the SDK wheel URL returns 403. Existing
> virtualenvs work. See [FLAWS.md](FLAWS.md) O7.

### The shared package

`shared/` holds one definition of the code every agent needs: attachment
extraction, Atlassian Document Format, Confluence reading, the environment
readers, and the result contract. Each project carries a **mirror** at
`src/shared/`, because the Aetherion packer builds each tarball from that
project's own `src/`.

```bash
python scripts/sync_shared.py          # after editing shared/
python scripts/sync_shared.py --check  # what CI should run
```

**Never edit `<project>/src/shared/`.** A test in each project fails if you do.

### The contract fixtures

The router is tested against contracts the children **actually produced**, each
captured by running that child in its own virtualenv:

```bash
python integration/capture_contracts.py          # re-capture
python integration/capture_contracts.py --check  # fail if a child's output drifted
```

---

## Publishing

One command, from inside the agent:

```bash
cd jira-task-creation
uv run python scripts/publish.py --bump patch
```

Seven gates: shared mirror in sync → tests → lint → all three version locations
agree → version raised → preflight → publish. `--dry-run` runs every check
without uploading.

**Children before the router.** A router dispatching to an agent that is not
live fails visibly; children published early are simply not yet routed to.

Two rules the pipeline exists to enforce, both from real incidents:

- **Publishing over an existing version can leave the old build running** while
  reporting success.
- **`202 Accepted` means the upload was received, not that the worker
  restarted.** Always confirm from the logs.
