# Phases

Eight phases, 0 through 7, done in order. Each one ends green — tests, lint and
formatting all passing — before the next begins.

Defects found along the way are recorded in [FLAWS.md](FLAWS.md) — what broke,
what caused it, and whether it is fixed. New flaws are appended there as they
are found, never quietly dropped.

**Status at a glance**

| # | Phase | State |
|---|---|---|
| 0 | Extract the shared foundation | ✅ **Complete** |
| 1 | Requirement Review: full context, @Aetherion trigger, completeness | ✅ **Complete** |
| 2 | Task Creation: become a pure function | ✅ **Complete** |
| 3 | Agent-to-agent communication (docs only, no production code) | ✅ **Complete** |
| 4 | Build Jira Orchestration (the router) | ✅ **Complete** |
| 5 | Integration and webhook migration | ✅ **Complete** |
| 6 | Publish and test all three | ⬜ Next |
| 7 | Real-world scenarios and correctness | ⬜ Not started |

**Test counts** — the number that must never go down:

| Agent | Folder | Registered as | Version | Before | Now |
|---|---|---|---|---|---|
| Work Breakdown | `jira-task-creation/` | `JiraTaskCreation` | 4.3.0 | 853 +1 skip | **917 +1 skip** |
| Requirement Review | `jira-requirement-review/` | `JiraRequirementReview` | 2.2.0 | 111 | **209** |
| Jira Orchestration | `jira-orchestration/` | `JiraOrchestration` | 0.1.0 | — (new) | **180** |
| | | | | **964** | **1,306** |

Every project is green on tests, `ruff` and `black`, and every publish pipeline
passes its dry run. Nothing has been published.

> **Note:** the SDK wheel URL now returns 403, so a *fresh* `uv sync` fails.
> Existing virtual environments work and all 1,306 tests pass through them. See
> [FLAWS.md](FLAWS.md) O7 — it blocks a clean checkout and CI, not the work so far.

The folders were renamed after Phase 0 to match what each agent does. The
**registered agent names are unchanged**, because those are what the Jira
Automation rule and the platform registry dispatch on — renaming registers a
*new* agent and leaves the old one live. That is queued for Phase 6, where the
rule is being rewired anyway.

### Where things live

```
JiraAgent/
├── PHASES.md               this file — what is done, what is next
├── PLAN.md                 the architecture and why it is shaped this way
├── FLAWS.md                every defect found, its cause, and its status
├── docs/AGENT_TO_AGENT.md  how one agent calls another on this platform
├── docs/a2a_proof_of_concept.py   runnable; proves the contract crosses intact
├── shared/                 ONE definition of the common code — never published
├── scripts/                sync_shared.py, publish.py
├── jira-task-creation/     creates Jira issues
├── jira-requirement-review/  reviews requirements, creates nothing
└── jira-orchestration/     the router — one webhook, one reply
```

---

## Phase 0 — Extract the shared foundation ✅

**Goal.** asurint needs to read attachments and Confluence. JiraTaskCreation
already did both, well and with tests. Copying that would have been the single
biggest duplication in the project, so it was extracted instead.

**Delivered**

- `shared/` — one canonical definition of 9 modules (~1,540 lines), mirrored
  into each agent's `src/shared/` by `scripts/sync_shared.py`.
- A `Transport` port so shared code does HTTP without knowing whether it is
  holding `httpx` (JiraTaskCreation) or `aiohttp` (asurint). Neither agent
  changed HTTP library.
- The whole Confluence reader moved behind that port — JiraTaskCreation's
  493-line Confluence test file passed **unchanged**.
- `contract.py`, the result contract Phases 2 and 4 depend on.
- Drift tests in both agents that fail if a mirror is edited directly.

**Proved, not assumed**

- 39 differential cases (old implementation vs new): 0 differences.
- The drift guard was made to fail on a deliberate edit, then pass when reverted.
- Both real tarballs inspected: `shared/` ships, no `__init__.py` leaks.
- Duplication scan: every shared capability has exactly one definition.

**Fixed along the way**

- asurint's ADF reader flattened whole tickets onto one line, which hid the
  bullet structure of acceptance criteria from an agent whose job is to notice
  missing acceptance criteria.
- asurint's `requires-python` allowed 3.13, so a plain `uv run pytest` could not
  resolve at all.
- asurint's `pyproject.toml` (0.0.22) and `metadata.json` (2.0.0) disagreed —
  the platform registers the metadata one, so the UI had been reporting a
  version nobody had deployed.

---

## Phase 1 — Requirement Review: full context, trigger, completeness ✅

**Rule kept: ADD, do not replace.** Manual single-issue, JQL batch, draft-only
default, the `.docx` report, all nine finding categories, readiness and score,
`PostJiraReviewComment` — all unchanged, and all 136 original tests still pass.

| | Task | Delivered |
|---|---|---|
| 1.1 | Defaults bug | `"default"` keys added to the metadata (booleans rendered **unchecked** without them, submitting an explicit `false` that overrode the code's fallback). One `config.TRIGGER_DEFAULTS` table now feeds both the code and the metadata, with a test that fails if they drift — or if a trigger's help text contradicts its own default. |
| 1.2 | Full context | `AttachmentsSource` (PDF, Word, Excel, PowerPoint, CSV, JSON, text, images) and `ConfluenceSource` (linked pages **and their attachments**), both through the Phase 0 shared code over the `aiohttp` transport adapter. Pages linked via Jira's own "Link → Confluence page" panel are found too — they live on the `remotelink` endpoint, invisible to anything reading the issue fields. |
| 1.3 | Webhook path | `mode="delegated"` returns the shared result contract and writes **nothing**. `post_to_jira` and `generate_docx` are forced off there regardless of payload or configuration. One implementation, one mode flag — the review itself cannot drift between paths. |
| 1.4 | Completeness | `sources_read` and `sources_missing` on every run, always present. Every gap reads as an instruction: *"FL-130 has no acceptance criteria — please add them"*, *"spec.xlsx could not be read — please re-attach it unprotected"*. |

**Obstacle cleared.** `budget.py::_priority` was an `if/elif` ranking partly on
label prefixes — which had already caused one bug, since "Target … (linked
issues)" starts with "Target" just like the requirement. It is now a table keyed
purely on document `kind`, with `NEVER_DROP` guaranteeing the requirement
survives any budget pressure and an unregistered kind ranking below everything
rather than above it.

**Bug found while wiring.** `has_requirement` only counted a non-empty
*description*, so a ticket reading "see the attached spec" aborted with "nothing
to review" while the spec sat in an attachment already downloaded. Now
`requirement_is_present()` treats attachments and Confluence pages as
requirement-bearing.

**Not verified.** These are unit tests against fake transports. No live Jira or
Confluence call has been made — that is Phase 7.

---

## Phase 2 — Task Creation: become a pure function ✅

**Delivered.** `mode="delegated"` still creates the Jira hierarchy — that is the
agent's purpose — but posts no reply, stamps no label, records no answered
comment and sends no email. It returns `shared.contract.AgentResult` instead.
Direct and webhook modes are byte-for-byte unchanged; all 878 existing tests
pass untouched.

**The design that made it small.** There are nine write-back sites and eight
notify sites across twelve exits. Guarding each would mean editing every outcome
branch — exactly what Open/Closed forbids — and a branch missed in that edit
would post a second comment in production *without failing a test*. So the mode
gates the two write functions (`_report`, `_notify`) through a `ContextVar`, and
one translation at the boundary shapes the result. Twelve exits, zero changes.

**A real bug, caught by its own test.** Setting the ContextVar only on the
delegated path left it true for whatever ran next in the same context, silently
switching write-back off for a direct run. Invisible — nothing errors, the reply
simply never appears. Now set on every run and reset in a `finally`.

**Guarantees, and where each now lives** — recorded in full in
`ARCHITECTURE.md` Part 5b:

| Guarantee | Owner |
|---|---|
| Duplicate protection, idempotency label | **unchanged — the agent** |
| Answered-comment property, marker label | **the router** (only it knows the reply posted) |
| Never email an invalid request | **the router** — and the child cannot even express the recommendation: `_EMAIL_KIND` has no `invalid_request` entry |
| The four outcomes | the agent, mapped to the contract's closed set; an unknown status fails safe to `FAILED` |

---

## Phase 3 — Agent-to-agent communication ✅

**No production code**, as specified. Output is
[docs/AGENT_TO_AGENT.md](docs/AGENT_TO_AGENT.md) plus a runnable proof of
concept, `docs/a2a_proof_of_concept.py`.

**The mechanism.** A child agent is a Temporal **child workflow**, started with
`agentExecutor.execute(name, payload, **options)`. Only six Temporal options are
accepted. The name is the *registered agent name* — which is why those were not
renamed when the folders were.

**The contract crosses intact** — verified by encoding a real `AgentResult`
through the platform's own converter: `json/plain`, 738 bytes, identical on the
way back, `outcome` still a real enum. Payloads are AES-GCM encrypted, and
oversized ones offload to Redis rather than failing.

**One subtle trap, found and documented.** Returning the dataclass instead of
`.to_dict()` encodes without complaint and decodes as a plain `dict`, with
`outcome` as a string. Nothing raises; the router just composes the wrong reply.
Both agents already return `.to_dict()`.

**Your sync position confirmed.** `execute` awaits the child, which is exactly
right: whoever waits for the result must be whoever posts the reply, or the
"two writers" problem returns. The Execution Mode toggle on the review form is
the *manual* path's setting and does not apply to routing.

**A Phase 2 defect this review caught.** Both agents had a version lookup that
read `metadata.json` at call time — file I/O *inside a workflow*, the same class
as the `RestrictedWorkflowAccessError` that has taken this service down before.
The existing sandbox tests only checked `os.environ`, so it passed. Both now use
an `AGENT_VERSION` constant, written by `scripts/publish.py` alongside the other
two versions, with a drift test and a new sandbox test that refuses any file read
from a workflow module.

**Five things could not be confirmed** without a live worker — listed explicitly
at the end of the document. The most important: *a cross-agent invocation has
never actually been run*. That is the first thing to prove in Phase 6, before
the router is wired to the webhook.

---

## Phase 4 — Jira Orchestration (the router) ✅

The router exists, at `jira-orchestration/`, with **98 tests**.

**Routing is a table.** `routing/catalog.py` holds the agents; adding a fourth
is one `AgentSpec` entry plus that agent returning the contract. No router code
changes — and the composer is proven not to be an obstacle by a test that feeds
it a synthetic agent it has never seen.

**Three layers, with asymmetric thresholds.** Work Breakdown needs 0.80 because
it writes to Jira; Requirement Review needs 0.60 because its only write is a
comment. Layer 1 costs nothing and is checked first; Layer 3 asks rather than
guessing, including when the classifier is unreachable.

**The invariant, tested directly: one user comment produces exactly one reply.**
Every path counts its posts — including a dead child, a malformed contract, and
an unreachable model. Dropped comments post nothing at all.

**Determinism is enforced, not hoped for.** `tests/test_workflow_sandbox.py`
fails if a workflow module imports anything that reads the environment, or calls
`uuid4()`, or reads the clock, or opens a file. The `run_id` is minted in the
ingress activity for exactly this reason.

**Every test Phase 4 required, passing:** the envelope on every outcome; a
router-only reply naming no child; a BOTH route naming both in order in one
comment; the composer working for an unknown agent; the same `run_id` in the
comment, the child call and the result; the footer never matching the mention
filter; exactly one comment on every path including failures.

**Two flaws found in my own work**, both recorded in FLAWS.md: Layer 1 matched
bare interrogatives and mis-routed chatter (F13), and the dispatch condition
inspected a *reason string* to decide whether a verb had been found — so the
classifier was never called at all (F15).

Docs: `README.md`, `ARCHITECTURE.md` (the reply-ownership decision and why
agents-posting-their-own was rejected), `BEHAVIOUR.md` (every reply, word for
word).

---

## Phase 5 — Integration and webhook migration ✅

One webhook, pointed at Jira Orchestration. Both children stay independently
invokable, and the review agent keeps its UI/CLI/API path as its primary one.

**The router is now tested against what the children really return.**
`integration/capture_contracts.py` runs each child **in its own virtualenv** —
necessary, because two of them define a top-level `agent` package and importing
both into one process shadows one with the other (verified). Nine real contracts
were captured, covering all six outcomes from both children, and
`--check` fails if a child's output ever drifts from them. Proven by making it
fail on a deliberate edit, then pass when restored.

**82 integration tests**, each proving one Phase 5 guarantee against real child
output rather than fixtures I wrote:

| Guarantee | How it is proven |
|---|---|
| Exactly one reply per user comment | every one of the 9 real contracts posts once |
| Envelope and attribution on every path | signature, Handled by, Routed to, run id, footer — on all 9 |
| At most one email | no reply mentions more than one |
| Never email an invalid request | the real `NOT_A_REQUIREMENT` contract carries `invalid_request` as what it *would* have sent; the router refuses it, **and** the child already refuses to recommend it |
| Duplicate protection survives the hop | the already-exists contract still names FL-9 and its 0.81 score; a redelivery never reaches the child |
| Labels and answered-comment tracking | asserted on the `post_reply` arguments for all 9 |
| No reply can retrigger the system | every line of every real reply checked against the mention filter |

**Deliverables:** [MIGRATION.md](MIGRATION.md) — the exact Automation rule
change (three things change, and `webhookEvent` is one of them, because it makes
an *edited* comment count as a new request), a seven-step verification on a real
ticket, and a rollback that is a rule change rather than a deployment: **under a
minute, no republish**.

Plus a system-level [README.md](README.md) covering all three agents.

---

## Phase 6 — Publish and test all three ⬜

**Step 1 is not publishing. It is proving that one deployed agent can call
another** — see [FLAWS.md](FLAWS.md) O3. Everything else in this project rests
on that one unverified assumption, and the fallback (merging the router into an
agent as an entry mode) is recorded at the end of
[docs/AGENT_TO_AGENT.md](docs/AGENT_TO_AGENT.md).

**Order: children first, then the router.** A router dispatching to an agent
that is not live fails in a way users see; children published early are simply
not yet routed to.

`scripts/publish.py` already enforces the order of operations within one agent —
shared mirror in sync → tests → lint → all three version locations agree →
version raised → preflight → publish — and is green on all three projects.

**Two things must be resolved here:**

1. ~~**The `.env` blocker.**~~ **Resolved** ([FLAWS.md](FLAWS.md) F17, F19).
   `LTW_LOCAL_EXECUTION` is now `false`, and `jiraagentdemo.atlassian.net` is
   confirmed as a real test instance that is the intended target — publish
   `jira-task-creation` with `--force-preflight` while that remains true.
2. ~~**The agent-identity rename.**~~ **Done** ([FLAWS.md](FLAWS.md) F16).
   The agents are now `JiraTaskCreation`, `JiraRequirementReview` and
   `JiraOrchestration`. On publish this registers `JiraTaskCreation` as a
   **new** agent; the old `LaxmanTicketWise` registration is untouched, which is
   what keeps the MIGRATION.md rollback working.

**Measured baseline, before any publish:** the registry at `test.sbox.aetherion.io`
(tenant `test`) holds 11 agents, and **none of ours is among them** — not even
`LaxmanTicketWise`, despite PUBLISH.md documenting it as having run in
production. Either it was published to a different tenant/environment, or it has
since been removed. Worth resolving before assuming the router will find its
children.

Plus: a numbered runbook, a liveness check per agent (an upload returning `202`
does **not** mean the worker restarted), and the end-to-end test plan.

---

## Phase 7 — Real-world scenarios and correctness ⬜

At least 30 scenarios. For each: the exact comment typed, the layer that
classified it, the route, the expected "Handled by" and "Routed to" lines, the
expected Jira writes, the expected reply text, and whether an email is sent.

**A good part of this is already covered by unit tests** — the routing table,
the thresholds, the envelope, the failure modes. What Phase 7 adds is the half
that cannot be faked:

| Needs a live system | Why it cannot be unit-tested |
|---|---|
| A requirement only inside a PDF / Excel / Word / JSON attachment | real attachment URLs and real extraction |
| A requirement only on a linked Confluence page | real permissions, real `remotelink` shapes |
| An attachment that will not open | real Jira error responses |
| Two rapid comments on one ticket | a real race |
| The same comment redelivered by Jira | real redelivery behaviour |
| A child agent timing out or not deployed | a real worker |
| Jira rejecting a write halfway | real permissions |
| A project the agent may not write to | real permissions |

**Also to be tested here, and not yet anywhere:** prompt injection through the
comment body. The mention filter and the closed intent set limit the blast
radius, but nothing has yet tried to talk the classifier into a BUILD.

Run what can be run. **Report honestly what passed and what did not.** Never
report a pass that was not observed.

---

## Open items carried into the remaining phases

From [FLAWS.md](FLAWS.md), the six open entries:

**19 fixed, 4 open.** O1, O5 and O6 are now closed (F19, F16, F18). Every
remaining item needs either your infrastructure or a live worker.

| | What | Owner | Blocks |
|---|---|---|---|
| **O2** | No live Jira or Confluence call has ever been made | Phase 7 | — |
| **O3** | **A cross-agent invocation has never been run** | Phase 6 step 1 | everything |
| **O4** | Five platform behaviours unconfirmed | needs a live worker | — |
| **O7** | **The SDK host returns 403 for everything** — a fresh `uv sync` fails | **you** | a clean checkout, CI |

Every remaining open item needs either **your infrastructure** or **a live
worker**. Nothing is left that can be fixed from the code.

O3 is the one to take seriously. Everything built in Phases 0–4 is sound against
fakes; none of it has spoken to another deployed agent.
