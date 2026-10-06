# CodePulse — complete build prompt (standalone agent, edge-case hardened)

> Paste everything below the line into the coding agent. Nothing exists yet; this is a greenfield
> build of a standalone agent.

---

You are building a brand-new, **standalone** Aetherion agent called **CodePulse**. Nothing has been
built yet — no folder, no scaffolding, no code. You are starting from zero.

CodePulse is its own agent with its own lifecycle, its own configuration and its own deployment.
It is **not** a module of any existing agent and must not import from one. If you find a helper in
my other agents worth reusing, copy it in deliberately and say so — never create a runtime
dependency on another agent's package.

---

# PART 0 — ORIENTATION, DO THIS FIRST

## 0.1 Learn the platform conventions

I have working Aetherion agents at `~/AetherionAgentBuilding/JiraAgent/` — `jira-orchestration`,
`jira-task-creation`, `jira-requirement-review`, and a `shared/` package. Read them **for
conventions only**, not to extend them:

| Read | To learn |
|---|---|
| any `src/agent/*.py` | how `@agent` is declared, how a flow is structured, how timeouts and retries are configured |
| any `src/tools/tools.py` | how `@tool` is declared, async conventions, how errors are returned rather than raised |
| `shared/mailer.py` | a working SMTP helper and its repeat-suppression logic — copy the approach |
| `shared/settings.py` | config loading and secret masking (`settings.mask(secret, reveal=0)`) |
| any `pyproject.toml` | how an agent is packaged and what the SDK packer includes in the artifact |
| any `tests/` | test layout and naming style |
| `jira-task-creation/src/jira/trigger.py` ~line 98 | the precedent for persisting small state as a key-value document |
| `jira-orchestration/src/tools/tools.py` ~line 198 | **an existing bug.** A swallowed exception returned as a normal "no action" result, indistinguishable from a legitimate drop. The user got nothing and the failure was undiagnosable for weeks. Do not reproduce this shape anywhere. |

## 0.2 Then stop and report back

Before implementing, reply with:

1. The directory tree you intend to create.
2. The exact SDK version and the real `@agent` / `@tool` signatures, **quoted from my existing
   agents**, so I can confirm you read them rather than guessed.
3. How scheduling works on this platform — whether the SDK provides cron triggers natively, and if
   not, what you propose instead.
4. How persistent storage works for an agent here. State is load-bearing for CodePulse; if the
   platform gives you nothing durable, say so immediately — that changes the design.
5. Anything in this prompt you believe is wrong, impossible, or a bad idea.

**Do not start implementing until I confirm.** A spec this size invites writing the interesting
parts first. The interesting parts are worthless without the boring ones.

---

# PART 1 — WHAT CODEPULSE IS

Twice a day, CodePulse scans a GitHub organisation, finds performance and code-health issues,
writes a `.docx` report, and emails it.

```
Schedule      08:30 and 20:30 Asia/Kolkata
              = cron "0 3 * * *" and "0 15 * * *" UTC
Weekly sweep  Sunday 03:00 UTC (08:30 IST)
Recipient     kothakota.sailaxmanrao@calfus.com  (configurable)
```

Store every timestamp in state as **UTC**. Convert to IST only in the display layer.

## Hard constraints — not negotiable

1. **Read-only, absolutely.** Never pushes, never opens a PR, never comments, never creates a
   branch, never writes to any scanned repository. The token must be read-only scope and the code
   must contain no write call to the GitHub API. Add a test that greps the source for write verbs.

2. **Never emit a secret value.** Not in the document, not in the email, not in logs, not in an
   exception message, not in a stack trace. If a scan *finds* a hardcoded secret, report the file,
   the line, and the fact. **Never the value**, never a prefix, never a redaction that reveals its
   length.

3. **Secrets never ship inside the deploy package.** The SDK packer copies root files by default
   and `.env` is not on its ignore list in my existing agents. Verify the built artifact contains
   no `.env`, no token, no password. This is testable and it is an acceptance criterion.

---

# PART 2 — PROJECT STRUCTURE

```
~/AetherionAgentBuilding/CodePulse/
├── pyproject.toml
├── README.md
├── .env.example                  # variable names only, never values
├── .gitignore                    # must include .env
├── docs/
│   ├── ARCHITECTURE.md           # scan modes, state model, the diff
│   ├── CHECKS.md                 # one section per check: what it finds, its grade, its false positives
│   ├── EDGE_CASES.md             # Part 7 of this prompt, as implemented
│   └── OPERATIONS.md             # schedules, budgets, what to do when a run fails
├── src/
│   ├── agent/
│   │   ├── agent.py              # @agent entry point, schedule registration
│   │   └── run_flow.py           # orchestrates one run end to end
│   ├── tools/
│   │   └── tools.py              # @tool wrappers — thin, no business logic
│   ├── models/
│   │   └── schemas.py            # Finding, RepoState, RunResult, Diff, enums
│   ├── state/
│   │   ├── store.py              # load/save RepoState; atomic write; schema migration
│   │   ├── fingerprint.py        # fingerprint() and _normalise()
│   │   └── lock.py               # run lock, see 7.1
│   ├── scan/
│   │   ├── modes.py              # choose_mode(), context-file matching
│   │   ├── github.py             # repos, compare, contents; rate limits, pagination, retries
│   │   ├── scanner.py            # walks files, dispatches checks, enforces budgets
│   │   └── skiplist.py           # path exclusion, .codepulse-ignore parsing
│   ├── checks/
│   │   ├── base.py               # Check protocol + registry
│   │   ├── missing_index.py
│   │   ├── n_plus_one.py
│   │   ├── select_star.py
│   │   ├── leading_wildcard_like.py
│   │   ├── missing_pagination.py
│   │   ├── swallowed_exception.py
│   │   └── hardcoded_secret.py
│   ├── diff/
│   │   └── classify.py           # NEW / UNCHANGED / WORSE / BETTER / FIXED + the guard
│   ├── report/
│   │   ├── document.py           # builds the .docx
│   │   └── email.py              # subject, body, attachment, SMTP
│   └── config.py                 # env parsing, defaults, startup validation
├── tests/
│   ├── test_fingerprint.py
│   ├── test_diff_classify.py
│   ├── test_modes.py
│   ├── test_scanner_budgets.py
│   ├── test_edge_cases.py        # one test per case in Part 7
│   ├── test_checks_*.py
│   ├── test_report.py
│   └── fixtures/                 # small synthetic repos as file trees
└── scripts/
    ├── local_run.py              # scan a local directory: no GitHub, no email
    └── preflight_publish.py      # verify no secrets in the artifact before packaging
```

`scripts/local_run.py` matters more than it looks — it is how you develop and how I test without
burning rate limit or mailing myself.

---

# PART 3 — PROBLEM 1: DEDUPLICATION IS THE PRODUCT

The 08:30 run finds 40 issues. The 20:30 run finds the same 40. By day three the email is 240 lines
and nobody opens it. An unread report is a failed product regardless of how good its findings are.

## 3.1 Fingerprint on content, never on line number

A line number changes when someone adds an import above. The finding is the same finding. If the
fingerprint moves, the diff reports one issue as `FIXED` **and** `NEW` in the same run — wrong
twice.

```python
def fingerprint(repo: str, path: str, check_id: str, snippet: str, ordinal: int = 0) -> str:
    basis = f"{repo}|{path}|{check_id}|{_normalise(snippet)}|{ordinal}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _normalise(snippet: str) -> str:
    """Collapse whitespace, strip comments.
    KEEP string literals and identifiers — they are what makes this finding
    this finding."""
```

**In the basis:** repo, path, check_id, normalised snippet, and `ordinal` (see 7.12).
**Never in the basis:** line number, commit SHA, run timestamp, severity.

```python
# run 1 — line 142
users = User.objects.filter(name__icontains=term)      # fp = a3f91c7d2e8b4105
# twelve import lines added above
# run 2 — line 154, identical statement
users = User.objects.filter(name__icontains=term)      # fp = a3f91c7d2e8b4105
```

Same fingerprint → `UNCHANGED`.

## 3.2 State, one document per repo

In the agent's own storage. **Never committed to a scanned repository.**

```json
{
  "schema_version": 1,
  "repo": "org/billing-api",
  "repo_id": 412993,
  "last_commit": "9f2c1a4",
  "default_branch": "main",
  "last_full_scan": "2026-09-28T03:00:00Z",
  "last_run": "2026-10-03T03:00:00Z",
  "last_run_status": "ok",
  "last_mode": "INCREMENTAL",
  "checks_hash": "c41e0b92",
  "consecutive_misses": 0,
  "findings": {
    "a3f91c7d2e8b4105": {
      "check": "missing-index", "severity": "high", "grade": "CONFIRMED",
      "path": "models/user.py",
      "first_seen": "2026-09-28T03:00:00Z",
      "last_seen": "2026-10-03T03:00:00Z"
    }
  }
}
```

- `last_run_status` ∈ `ok | error | budget_exhausted`. Anything but `ok` forces `FULL` next run.
- `checks_hash` hashes the enabled check ids **and versions**. A mismatch forces `FULL`.
- `repo_id` is GitHub's numeric id — it survives a rename (7.7).
- Keyed by fingerprint, so lookup is O(1).

## 3.3 Report the diff, not the list

| Class | Condition |
|---|---|
| `NEW` | fingerprint not in stored state |
| `UNCHANGED` | in both, same severity |
| `WORSE` | in both, severity escalated |
| `BETTER` | in both, severity reduced |
| `FIXED` | in stored state, absent now — **and the guard passes** |

`NEW`, `FIXED`, `WORSE` get full detail. `UNCHANGED` collapses to one line plus an appendix.

```
NEW (3) · FIXED (5) · WORSE (1) · UNCHANGED (34, see Appendix A)
```

## 3.4 ★ THE GUARD — the most important rule in this build ★

> **A finding may be marked `FIXED` only if its file was conclusively examined in this run.**

"Conclusively examined" means exactly one of:

```python
def may_mark_fixed(finding, run) -> bool:
    return (
        finding.path in run.files_read              # read, finding no longer present
        or finding.path in run.files_deleted        # deleted in the diff — genuinely gone
        or (run.mode in (FULL, REPO_FULL)
            and run.repo_scan_ok
            and finding.path not in run.files_present)   # file no longer in the tree
    )
```

Everything else carries forward as `UNCHANGED`.

Why it exists: if `billing-api` fails to clone, its twelve findings are simply absent from the
result set. A naive diff calls them `FIXED`, drops them from state, and they return as `NEW`
tomorrow. That oscillation destroys trust in the report permanently — and once an engineer stops
believing it, every later finding is wasted work.

Note the second and third clauses carefully. Without them, a finding in a **deleted** file is never
marked fixed and haunts the report forever. The guard must be precise in both directions.

Every report carries a **"Not scanned this run"** section naming each skipped repo and why. Omit
only when empty.

---

# PART 4 — PROBLEM 2: A STATIC SCAN CANNOT PROVE A QUERY IS SLOW

It has not run the query. It does not know whether the table holds 300 rows or 300 million. It may
not have read the migration that already adds the index. **One wrong claim and the whole document
is ignored, permanently.**

The fix is not smarter analysis. It is honest labelling, enforced by schema.

## 4.1 Two grades, required field

`grade` is a **required** enum: `CONFIRMED | SUSPECTED`. Validate with Pydantic strict mode. Reject
any finding missing it.

**CONFIRMED** — provable from repository contents alone:

```
CONFIRMED · HIGH · missing-index · org/billing-api

  models/user.py:142 queries User.name with __icontains.
  No index on users.name exists anywhere in migrations/.
  Both facts are present in the repository. No assumption is made.

  Fix:
    CREATE INDEX CONCURRENTLY idx_users_name_trgm
      ON users USING gin (name gin_trgm_ops);
    -- requires: CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

**SUSPECTED** — depends on information the code does not contain:

```
SUSPECTED · MEDIUM · n-plus-one · org/orders-api

  views.py:88 iterates order.items and accesses item.product inside the
  loop. If the queryset is not prefetched upstream this is one query per
  item.

  Why suspected:
    the queryset is constructed in a service layer this scan did not
    resolve; it may already call prefetch_related("items__product").

  How to verify:
    run the endpoint with django-debug-toolbar, or wrap the view in
    len(connection.queries). If the query count scales with the number of
    items, it is real.
```

## 4.2 The two rules that give the grade meaning

**Rule A — every `SUSPECTED` finding must carry a non-empty `how_to_verify` with a concrete command
or tool.** No verification step → the finding does not ship. This eliminates hand-waving, because a
model cannot write a specific verification step for something it invented.

**Rule B — `SUSPECTED` never drives the headline.** The subject counts confirmed only:

```
CodePulse 03 Oct 08:30 — 3 confirmed, 7 suspected, 5 fixed (31/40 repos)
```

## 4.3 Severity

`CRITICAL | HIGH | MEDIUM | LOW`, assigned by the **check module**, never by a model. A module that
cannot justify severity deterministically assigns `MEDIUM`.

---

# PART 5 — PROBLEM 3: SCANNING AN ORG TWICE A DAY IS EXPENSIVE

First run scans everything; after that only what changed; once a week everything again. Correct —
but "what changed" cannot mean "the files in the diff" alone. Four modes, chosen **per repo, per
run**.

## 5.1 Mode selection — first match wins

| Mode | Triggered when | Reads |
|---|---|---|
| `FULL` | no state (**first run**) · weekly sweep · `checks_hash` changed · `schema_version` changed · `last_run_status != "ok"` · `compare` unusable (7.6) | every eligible file |
| `REPO_FULL` | the diff touches any **context file** (5.2) | every eligible file in that repo |
| `INCREMENTAL` | the diff touches only ordinary source files | changed files + one hop of importers |
| `SKIP` | `compare` returns zero changed files | nothing; one API call |

```python
def choose_mode(repo, state, diff, run) -> Mode:
    if state is None:                               return Mode.FULL   # first run
    if run.is_weekly_sweep:                         return Mode.FULL
    if state.checks_hash != current_checks_hash():  return Mode.FULL
    if state.schema_version != SCHEMA_VERSION:      return Mode.FULL
    if state.last_run_status != "ok":               return Mode.FULL
    if diff is None:                                return Mode.FULL   # compare failed
    if not diff.files:                              return Mode.SKIP
    if any(is_context_file(f) for f in diff.files): return Mode.REPO_FULL
    return Mode.INCREMENTAL
```

Delta from one call:

```
GET /repos/{org}/{repo}/compare/{last_commit}...{default_branch}
```

## 5.2 Context files — why "changed files only" is wrong

**A change in one file can create or resolve a finding in a file that did not change.**

- `migrations/0042_add_index.py` is added. It **fixes** the `missing-index` in `models/user.py` —
  but that file is not in the diff, so an incremental scan never re-reads it, the finding never
  clears, and the report shows a solved problem as open indefinitely.
- A service layer gains `prefetch_related()`. It resolves an `n-plus-one` in an unchanged view.
- An index is dropped. It **creates** a finding in a model file nobody touched.

```python
CONTEXT_FILE_PATTERNS = (
    "**/migrations/**",                               # schema — drives missing-index
    "**/schema.sql", "**/*.ddl",
    "**/models.py", "**/models/**", "**/entity/**",   # ORM definitions
    "requirements*.txt", "pyproject.toml", "poetry.lock",
    "package.json", "pom.xml", "build.gradle",
    "**/settings.py", "**/application*.yml", "**/*.config.*",
    ".codepulse-ignore",
)
```

In **config**, not hardcoded — teams extend it per stack. These files change rarely, so `REPO_FULL`
fires a few times a week per repo, not every run.

## 5.3 Blast radius inside `INCREMENTAL`

For each changed file, also re-scan files in the same package that import it. **One hop, not
transitive** — transitive closure degenerates into a full scan. If import resolution is unavailable
for a language, fall back to same-directory.

## 5.4 The consequence to accept deliberately

A fix in an unscanned file is reported as resolved at the next `FULL` or `REPO_FULL` run, not
immediately. Worst case is the weekly sweep — seven days.

That is the correct trade. A late "fixed" is an annoyance; a false "fixed" is fatal. The
context-file rule means the common fixes clear on the very next run anyway.

Say it in the document footer:

```
Findings in files not read this run are carried forward unchanged.
Next full sweep: Sunday 05 Oct 08:30 IST.
```

## 5.5 Weekly sweep

Sunday 03:00 UTC: every repo in `FULL`, `last_commit` ignored, state rebuilt. Catches drift — a
check added on Tuesday that never saw an unchanged file, and fixes in files incremental runs
skipped.

Budgets still apply. If the sweep cannot finish it persists its cursor and the 20:30 run continues
it. The sweep is not marked complete until every repo is covered.

## 5.6 Budgets with a persisted cursor

```python
MAX_REPOS_PER_RUN  = 40
MAX_FILES_PER_REPO = 800
MAX_FILE_BYTES     = 400_000
MAX_RUN_SECONDS    = 900
MAX_FINDINGS_DETAILED = 150     # see 7.16
```

On exhausting a budget: **stop, say so**, persist a cursor, resume there next run, round-robin so
no repo is starved.

```
Budget: scanned 31 of 40 repos in 900s. Resuming from org/payments next run.
```

**A repo cut off by the budget is not marked scanned.** Its `last_commit` stays put,
`last_run_status = "budget_exhausted"`, forcing `FULL` next run. Without this its unscanned commits
are skipped forever.

**The first run is expensive by definition.** Every repo in `FULL`. Expect it to exhaust the budget
and span several runs. The cursor handles it; the report says so.

## 5.7 Skip before parsing

```
vendor/  node_modules/  dist/  build/  target/  .venv/  __pycache__/
**/__generated__/**  **/migrations/*_auto_*.py
*.min.js  *.min.css  *.lock  *.svg  *.snap  *.map
```

Plus each repo's `.codepulse-ignore`: `.gitignore` syntax for paths, plus a fingerprint list.

```
vendor/
a3f91c7d2e8b4105  # intentional: table is <500 rows, index not worth the write cost
```

Suppressed findings are counted, never detailed: `7 findings suppressed.`

---

# PART 6 — CHECK MODULES

Pluggable. **Adding a check must require zero changes to the scanner, the diff engine or the report
builder.** If it forces a change to any of those three, the abstraction is wrong — fix it.

```python
class Check(Protocol):
    check_id: str                 # stable; part of the fingerprint — NEVER rename
    version: str                  # bump when detection logic changes → forces FULL
    languages: frozenset[str]
    default_grade: Literal["CONFIRMED", "SUSPECTED"]

    def run(self, file: SourceFile, ctx: RepoContext) -> list[Finding]: ...
```

`RepoContext` carries what a check needs beyond one file — the migration set, the dependency
manifest, the ORM in use. It is what lets `missing-index` reach `CONFIRMED` rather than `SUSPECTED`.

| `check_id` | Default grade | Detects |
|---|---|---|
| `missing-index` | CONFIRMED when migrations readable, else SUSPECTED | filtered/ordered column with no index |
| `n-plus-one` | SUSPECTED | ORM access inside a loop |
| `select-star` | CONFIRMED | `SELECT *` in raw SQL |
| `leading-wildcard-like` | CONFIRMED | `LIKE '%x%'` / `__icontains` — no b-tree index possible |
| `missing-pagination` | SUSPECTED | list endpoint with no limit/offset |
| `swallowed-exception` | CONFIRMED | `except: pass`, empty `catch {}` |
| `hardcoded-secret` | CONFIRMED | file + line + fact **only**, never the value |

**A check that cannot decide returns nothing. Silence beats a false positive.**

Document each in `docs/CHECKS.md`: what it looks for, why its grade is what it is, and its known
false-positive modes.

---

# PART 7 — EDGE CASES AND FAILURE MODES

**This part is the difference between a demo and an agent that runs unattended for a year.** Each
case below has a required behaviour. Implement every one, and write a test per case in
`tests/test_edge_cases.py`. Record what you implemented in `docs/EDGE_CASES.md`.

## Scheduling and concurrency

**7.1 Two runs overlap.** The 08:30 run is still going when 20:30 fires — a slow first run makes
this likely. Take a **run lock** with a holder id and a timestamp. A second run that finds a live
lock exits immediately and logs it; it does **not** queue and does not email. A lock older than
`MAX_RUN_SECONDS × 2` is treated as stale and broken, because a crashed run leaves one behind.

**7.2 A run is missed entirely** (agent down, platform outage). Nothing special is required and
that is the point: the delta is computed from `last_commit`, **never from elapsed time**. The next
run picks up every commit since the last successful scan, however long ago. Do not write any logic
that assumes runs are twelve hours apart.

**7.3 Clock skew / out-of-order runs.** Never order anything by wall-clock comparison between runs.
State transitions key on `last_commit` and explicit status fields only.

**7.4 Timezone.** `Asia/Kolkata` has no DST, but use `zoneinfo` properly anyway — the recipient
timezone is configurable and the next one may have DST. Cron is registered in UTC. All stored times
are UTC. Never store a local timestamp.

## GitHub API

**7.5 Rate limiting.** Two separate kinds, handled differently:
- *Primary* (`X-RateLimit-Remaining: 0`): stop issuing requests, read `X-RateLimit-Reset`, end the
  run cleanly, mark unscanned repos `error`, report "rate limit reached, N repos not scanned,
  resets at HH:MM".
- *Secondary / abuse detection* (`403` with `Retry-After`): exponential backoff with jitter,
  honouring `Retry-After`, max 3 attempts, then treat as above.

Never retry into a rate limit in a tight loop. Budget API calls: check remaining quota before
starting each repo and stop early if it would exhaust it.

**7.6 `compare` fails because history was rewritten.** A force-push or rebase makes `last_commit`
unreachable and `compare` returns 404. **Fall back to `FULL` for that repo** and log it. Do not
treat it as an error, and do not silently scan nothing.

**7.7 Repo renamed.** GitHub redirects, so the old name still resolves — but your state is keyed by
name. Store `repo_id` (numeric, stable) and reconcile by id at the start of each run. On a detected
rename, migrate the state document to the new name and note it in the report.

**7.8 Repo deleted, archived, or made private/inaccessible.** Do not delete its state on the first
miss — a transient 404 happens. Increment `consecutive_misses`. After 3 consecutive misses, retire
the state document and report once: `org/old-service retired — not reachable for 3 runs`. Archived
repos are skipped by default with a config flag to include them.

**7.9 Empty repo, or no default branch.** A repo with zero commits returns 409 on most endpoints.
Skip it, count it, do not treat it as an error.

**7.10 Pagination.** The org repo list, the tree listing and `compare` are all paginated. Follow
`Link` headers to the end. A 40-repo org with no pagination handling silently scans 30 and you will
never notice. `compare` also truncates above 300 files — when `files` is truncated, **promote the
repo to `REPO_FULL`** rather than scanning a partial diff.

**7.11 Token expired or scope revoked.** Fail the whole run immediately with a clear message, email
the failure notice, and do not modify any state. A run that cannot authenticate must not mark
anything.

## Files and content

**7.12 Two identical findings in one file.** The same offending statement appears twice —
identical snippet, same path, same check. Without disambiguation both produce the same fingerprint
and one is lost. Add an `ordinal`: the 0-based index of this occurrence within the file, in order
of appearance. Accept the known cost — inserting a new occurrence *above* an existing one shifts
ordinals and churns those fingerprints. Document this in `docs/EDGE_CASES.md`; it is rare and the
alternative (losing findings) is worse.

**7.13 File renamed.** Path is in the fingerprint, so a rename produces a false `FIXED` + `NEW`
pair. The `compare` response marks renamed files with `previous_filename` — **use it to migrate the
stored fingerprints** (recompute with the new path, carry `first_seen` forward). In `FULL` mode
where no diff exists, a rename does produce the false pair; accept it and note it.

**7.14 File deleted.** Covered by the guard in 3.4 — a deleted path counts as conclusively
examined, so its findings are correctly `FIXED`. Without that clause they would haunt the report
forever.

**7.15 Undecodable or binary content.** Attempt UTF-8, then the charset the API reports, then skip.
Never crash on a decode error. Also skip: files over `MAX_FILE_BYTES`, Git LFS pointer files
(detect the `version https://git-lfs` header), submodule entries, and symlinks (do not follow —
a symlink loop will hang the run). Count each category and report the total: `184 files skipped
(binary 31, oversize 12, generated 141)`.

**7.16 A findings explosion.** The first `FULL` run on a legacy monorepo returns 4,000 findings. A
4,000-entry document is unreadable and may fail to build. Cap detailed findings at
`MAX_FINDINGS_DETAILED`, keep the highest severity and CONFIRMED first, and state plainly:
`4,012 findings; 150 shown in detail, full list attached as findings.json`. Never truncate without
saying so.

**7.17 Monorepo exceeds `MAX_FILES_PER_REPO`.** Keep a **per-repo file cursor** as well as the
per-org repo cursor, so the next run continues inside that repo instead of restarting it. A repo
only reaches `last_run_status = "ok"` when the whole tree was covered.

**7.18 Malformed `.codepulse-ignore`.** Bad glob, unparseable line, or a file so large it is
obviously wrong. Ignore the bad lines, apply the good ones, and report:
`org/x: .codepulse-ignore line 7 ignored (invalid pattern)`. Never let a repo's own file crash the
run — that is a denial-of-service on your own agent.

**7.19 A check module raises.** Isolate every `(file, check)` invocation. One crashing check must
not kill the file, the repo, or the run. Record it, and crucially: **that file is not "read" for
that check's purposes**, so findings from that check in that file carry forward rather than being
marked `FIXED`. Report: `n-plus-one failed on 3 files in org/orders-api`. A check that fails on
more than 20% of files in a run is disabled for the remainder and flagged loudly.

## State and storage

**7.20 Partial or interrupted state write.** Write to a temporary key and swap atomically, or
write-then-verify. A half-written state document is worse than no state — it produces wrong diffs
silently. On a corrupt or unparseable state document: log it, treat the repo as having no state
(→ `FULL`), and report `org/x: state rebuilt (previous state unreadable)`.

**7.21 Schema migration.** `schema_version` exists so that changing the state shape is safe. On a
version mismatch, either migrate explicitly or force `FULL` and rebuild. Never read an old shape
with new code and hope.

**7.22 ★ Email fails after state was saved ★** This one costs you real findings. If state is
committed and the email never arrives, those findings are `UNCHANGED` forever and **the user is
never told about them.** Required order:

```
scan → build document → send email → ONLY on successful send, commit state
```

If the send fails after retries, **do not commit state**. The next run re-reports everything,
which is noisy but correct. Noisy-and-correct beats quiet-and-wrong. Log the failure explicitly.

**7.23 Document build fails.** A `.docx` library error must not lose the run. Fall back to emailing
the full plain-text report with no attachment, and say the attachment could not be generated.
Apply 7.22's ordering to this path too.

**7.24 SMTP failures.** Retry with backoff (3 attempts). Distinguish a hard rejection (bad
recipient, auth failure — do not retry, log clearly) from a transient one (timeout, 4xx — retry).

## Configuration and data

**7.25 Bad configuration at startup.** Missing token, empty recipient list, unparseable cron,
invalid timezone, `MAX_*` set to zero or negative. Validate **all of it at startup** and fail
loudly naming the exact variable. Never discover a missing token halfway through a scan.

**7.26 Empty org, or every repo filtered out by allowlist/denylist.** Not an error. Send the quiet
email saying zero repos matched, so the silence is explained rather than ambiguous.

**7.27 A repo with no supported languages.** Skip cleanly, count it, do not warn per-file.

---

# PART 8 — OUTPUT

## 8.1 Document

1. **Header** — org, timestamp (IST and UTC), repos scanned / total, duration, mode breakdown:
   `FULL 2 · REPO_FULL 3 · INCREMENTAL 9 · SKIP 26`
2. **Summary** — counts by class and grade; the `UNCHANGED` roll-up
3. **NEW** — full detail; CONFIRMED before SUSPECTED; severity descending
4. **WORSE** — showing the previous severity
5. **FIXED** — one line each (repo · path · check · first seen)
6. **Not scanned this run** — repo and reason. Omit only when empty.
7. **Anomalies** — renames, retired repos, rebuilt state, disabled checks, skipped-file counts,
   truncation notices. Everything from Part 7 that happened, in one place.
8. **Appendix A — unchanged** — one line each
9. **Appendix B — suppressed** — count only
10. **Footer** — the carried-forward note and the next sweep date

Every finding renders: grade, severity, `check_id`, repo, path, the evidence, then the **fix**
(CONFIRMED) or **how_to_verify** (SUSPECTED), and `first_seen`.

## 8.2 Email

```
Subject: CodePulse {DD Mon HH:MM IST} — {n} confirmed, {n} suspected, {n} fixed ({scanned}/{total} repos)
```

Body: the summary block plus NEW findings as plain text. Document attached. Nothing else.

**A quiet run still sends an email.** Silence is ambiguous — the reader cannot tell "nothing found"
from "the agent is dead."

```
CodePulse 03 Oct 08:30 — 0 confirmed, 0 suspected, 2 fixed (40/40 repos)

No new findings. Two findings from the previous run are resolved:
  org/billing-api  models/user.py  missing-index   (open since 28 Sep)
  org/auth         views.py        swallowed-exc   (open since 01 Oct)

34 findings carried over unchanged. Full detail attached.
```

**A failed run also emails.** If the run cannot start — auth failure, no repos reachable, storage
unavailable — send a short failure notice naming the error. Never fail silently. That is exactly
the hole at `jira-orchestration/src/tools/tools.py:198`, where a swallowed exception returned a
result shape indistinguishable from "nothing to do".

---

# PART 9 — CONFIGURATION

| Variable | Meaning |
|---|---|
| `CODEPULSE_GITHUB_ORG` | organisation to scan |
| `CODEPULSE_GITHUB_TOKEN` | read-only PAT, `repo:read` scope, nothing more |
| `CODEPULSE_RECIPIENTS` | comma-separated |
| `CODEPULSE_TIMEZONE` | default `Asia/Kolkata` |
| `CODEPULSE_MAX_REPOS` / `_MAX_FILES` / `_MAX_RUN_SECONDS` / `_MAX_FINDINGS_DETAILED` | budgets |
| `CODEPULSE_CONTEXT_PATTERNS` | extends the context-file list |
| `CODEPULSE_REPO_ALLOWLIST` / `_DENYLIST` | optional |
| `CODEPULSE_INCLUDE_ARCHIVED` | default false |
| `CODEPULSE_DRY_RUN` | scan and build the document, send no email, **do not commit state** |

`.env.example` lists every name with an empty value and a comment. Never a real value.

---

# PART 10 — ACCEPTANCE CRITERIA

Each must pass as an **automated test**, not a manual check.

**Core**

1. Adding twelve blank lines above a flagged statement yields `UNCHANGED`, not `FIXED` + `NEW`.
2. A repo that fails to clone appears under "Not scanned this run", **none** of its findings is
   marked `FIXED`, and its `last_commit` is unchanged.
3. Every `SUSPECTED` finding has a non-empty, specific `how_to_verify`; one without it is rejected
   at validation, not rendered.
4. The subject's confirmed count equals the `CONFIRMED` count, unaffected by `SUSPECTED`.
5. Two runs with no commits between them: the second emails zero new findings and makes at most one
   API call per repo.

**Modes**

6. First run on a repo with no state selects `FULL`, whatever `compare` would return.
7. A commit adding only a migration selects `REPO_FULL`, and a `missing-index` in an
   otherwise-unchanged model file is correctly marked `FIXED`.
8. A commit touching one ordinary source file selects `INCREMENTAL` and **no finding in any unread
   file is marked `FIXED`**.
9. Changing the enabled check set (or a check's `version`) forces `FULL` on every repo.
10. Hitting `MAX_RUN_SECONDS` names the resume point, leaves that repo's `last_commit` untouched,
    sets `budget_exhausted`, and runs `FULL` next.

**Edge cases — one test each**

11. A deleted file's finding **is** marked `FIXED` (7.14).
12. A renamed file carries its finding forward with `first_seen` preserved, producing no
    `NEW`/`FIXED` pair (7.13).
13. Two identical statements in one file produce two distinct fingerprints (7.12).
14. `compare` returning 404 after a force-push falls back to `FULL`, not to an error (7.6).
15. A second run starting while the first holds the lock exits without emailing (7.1).
16. A stale lock older than `2 × MAX_RUN_SECONDS` is broken and the run proceeds (7.1).
17. A check raising on one file does not abort the repo, and that check's findings in that file
    carry forward rather than being marked `FIXED` (7.19).
18. **Email send failure leaves state uncommitted**, and the next run re-reports the same
    findings (7.22).
19. A corrupt state document causes a `FULL` rebuild and an entry in the Anomalies section (7.20).
20. A binary file, an LFS pointer and a symlink are each skipped without an exception (7.15).
21. Rate-limit exhaustion ends the run cleanly with unscanned repos reported, not retried in a
    loop (7.5).
22. A malformed `.codepulse-ignore` line is ignored, the valid lines still apply, and it is
    reported (7.18).
23. 4,000 findings produce a document capped at `MAX_FINDINGS_DETAILED` with an explicit truncation
    notice (7.16).
24. A `compare` response flagged as truncated promotes the repo to `REPO_FULL` (7.10).

**Safety**

25. A run that cannot start at all still emails a failure notice (8.2).
26. Searching the built artifact for the GitHub token and the SMTP password finds neither.
27. A test greps the source for GitHub write verbs (POST/PATCH/PUT/DELETE to repo endpoints) and
    finds none.
28. Renaming a `check_id` is caught by a test, because it invalidates every stored fingerprint.
29. `scripts/local_run.py` scans a fixture directory and produces a document with no network access
    and no email sent.

---

# PART 11 — BUILD ORDER

| # | Step |
|---|---|
| 1 | **State store + fingerprinting + diff engine, with the full guard (3.4).** Tests first. |
| 2 | GitHub client: repos, compare, contents — **with 7.5–7.11 handled from the start**, not bolted on. Mode selection, context files, budgets, cursors, skip-list. |
| 3 | **One** check module — `leading-wildcard-like`. Smallest, unambiguous, CONFIRMED. |
| 4 | Report builder + email, including quiet-run, failed-run, and the 7.22 commit ordering. |
| 5 | The run lock, schedule registration, two cron triggers plus the Sunday sweep. |
| 6 | `scripts/local_run.py`, `scripts/preflight_publish.py`. |
| 7 | The remaining six check modules. |
| 8 | `docs/ARCHITECTURE.md`, `docs/CHECKS.md`, `docs/EDGE_CASES.md`, `docs/OPERATIONS.md`, `README.md`. |

Steps 1, 2 and 4 are where the value is. Step 7 is where the temptation is.

**Stop after each of steps 1–5 and show me what you built before continuing.**

---

# PART 12 — CODE QUALITY

- **No dead code.** No unused constant, no function nothing calls, no commented-out block. A
  defined-but-never-called helper is a defect. (My existing agent had a documented, tested
  `lock_key()` that was never called from anywhere in `src/`.)
- **No duplication.** One home per constant and helper. My existing agents had `_mask()` defined
  three times.
- **Pure logic separated from I/O.** `fingerprint()`, `choose_mode()`, `classify()`,
  `may_mark_fixed()` and every check are pure functions over data — testable with no network, no
  filesystem, no clock. Inject the clock and the network.
- **Errors are returned, never swallowed.** Never catch broadly and return something that looks
  like a normal result. If you must catch broadly, the returned object carries a distinct `error`
  field the caller branches on. This single defect made my existing orchestration agent
  undiagnosable.
- **Every `.md` accurate at the end.** A README describing a flag that does not exist is worse than
  no README.
- **Tests before implementation** for steps 1 and 2.

---

# PART 13 — START HERE

1. Read the existing agents (0.1) for platform conventions.
2. Reply with the five items in 0.2.
3. Wait for my confirmation.
4. Build in the order in Part 11, stopping after each of steps 1–5.
