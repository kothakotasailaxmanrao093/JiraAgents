# CodePulse — Implementation Specification

**For:** the coding agent implementing this.
**Status:** build spec. Everything below is a requirement, not a suggestion.
**Platform:** Aetherion SDK, same conventions as `jira-orchestration` / `jira-task-creation` in `~/AetherionAgentBuilding/JiraAgent/`.

---

## 0. What CodePulse is

A scheduled agent that scans a GitHub organisation twice a day, finds performance and code-health
issues, writes a `.docx` report, and emails it.

**Schedule:** 08:30 and 20:30 Asia/Kolkata → cron `0 3 * * *` and `0 15 * * *` UTC.
**Recipient:** `kothakota.sailaxmanrao@calfus.com` (configurable).

**Read-only. Absolute.** CodePulse never pushes, never opens a PR, never comments on an issue,
never writes to any repository. It reads and it reports. Nothing else.

**Secrets rule.** Never log or include a token, secret or credential *value* anywhere — not in the
document, not in the email, not in logs. If a scan *finds* a hardcoded secret, report the file, the
line and the fact that a secret is present. Never the value. Secrets never ship inside the deploy
package (keep `.env` out of the artifact — the SDK packer copies root files by default, so verify).

---

## 1. The three problems this spec exists to solve

The naive version of this agent fails in three specific, predictable ways. Each has a fix below.
These are the highest-priority items in the build. **Build the state layer first** — the checks are
easy to add later; a deduplication scheme retrofitted onto a year of noisy emails is not.

| # | Failure | Fix |
|---|---------|-----|
| 1 | Same 40 findings emailed twice a day forever; nobody reads it | Content-hash fingerprints + per-repo state + report the diff |
| 2 | Agent claims "this query is slow" when it cannot know that | CONFIRMED / SUSPECTED split, verification step mandatory |
| 3 | Scanning 40 repos twice a day is slow and expensive | Four scan modes: full first run, delta after, repo-full on schema change, weekly sweep |

---

## 2. Fix 1 — Deduplication

### 2.1 Fingerprints are content-based, never line-based

A line number changes when someone adds an import above the code. The finding is the same finding.
If the fingerprint moves, the diff reports it as `FIXED` + `NEW` in the same run, which is a lie
twice over.

```python
def fingerprint(repo: str, path: str, check_id: str, snippet: str) -> str:
    """Stable across reformatting and line movement."""
    normalised = _normalise(snippet)
    basis = f"{repo}|{path}|{check_id}|{normalised}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def _normalise(snippet: str) -> str:
    """Collapse whitespace, strip comments. Keep string literals and
    identifiers intact — they are what makes the finding this finding."""
    ...
```

**Worked example.**

```python
# run 1 — line 142
users = User.objects.filter(name__icontains=term)      # fp = a3f91c7d2e8b4105
```

Someone adds twelve import lines above it.

```python
# run 2 — line 154, identical statement
users = User.objects.filter(name__icontains=term)      # fp = a3f91c7d2e8b4105
```

Same fingerprint → correctly reported as `UNCHANGED`, not as a new finding.

**Do include** in the basis: repo, file path, check id, normalised snippet.
**Do not include**: line number, commit SHA, run timestamp, severity.

### 2.2 State storage

Precedent already in the codebase: `jira-task-creation/src/jira/trigger.py:98` stores state as a
Jira issue property. Same principle — a small JSON document, written to the agent's own storage,
**never committed to any scanned repository**.

```json
{
  "schema_version": 1,
  "repo": "org/billing-api",
  "last_commit": "9f2c1a4",
  "last_full_scan": "2026-09-28T03:00:00Z",
  "last_run": "2026-10-03T03:00:00Z",
  "last_run_status": "ok",
  "last_mode": "INCREMENTAL",
  "checks_hash": "c41e0b92",
  "findings": {
    "a3f91c7d2e8b4105": {
      "check": "missing-index",
      "severity": "high",
      "grade": "CONFIRMED",
      "path": "models/user.py",
      "first_seen": "2026-09-28T03:00:00Z",
      "last_seen": "2026-10-03T03:00:00Z"
    }
  }
}
```

One state document per repo. Keyed by fingerprint, so lookup is O(1).

`last_run_status` ∈ `ok | error | budget_exhausted`. Anything other than `ok` forces `FULL` mode
for that repo on the next run (§4.1). `checks_hash` is a hash of the enabled check-module ids and
versions; a mismatch also forces `FULL`.

### 2.3 The diff is the report

Classify every fingerprint in the current run against the stored state:

| Class | Condition |
|-------|-----------|
| `NEW` | fingerprint not in stored state |
| `UNCHANGED` | in both, same severity |
| `WORSE` | in both, severity escalated since last run |
| `BETTER` | in both, severity reduced |
| `FIXED` | in stored state, absent now — **and the guard in 2.4 passes** |

The email and the document lead with `NEW`, `FIXED` and `WORSE` in full detail. `UNCHANGED`
collapses to one line plus an appendix:

```
NEW (3)          — full detail
FIXED (5)        — full detail
WORSE (1)        — full detail
UNCHANGED (34)   — 34 open findings carried over; see Appendix A
```

Nobody reads forty findings twice a day. Everybody reads "3 new, 5 fixed."

### 2.4 The partial-scan guard — non-negotiable

> **A finding may be marked `FIXED` only if the file it lives in was successfully read in this run.**

This guard does double duty. It covers the failed-clone case below, and it is also what makes
incremental scanning (§4) safe: in an incremental run most files are never opened, so their
findings are *absent from the result set* for a reason that has nothing to do with being fixed.
Implement it as a single check against the set of files actually read this run — never as a special
case per scan mode.

```python
FIXED  requires  finding.path in run.files_read
otherwise       carry forward as UNCHANGED
```

If `billing-api` failed to clone (rate limit, network, auth), its twelve findings are simply absent
from the current result set. A naive diff reports all twelve as `FIXED`, drops them from state, and
they reappear as `NEW` tomorrow. That oscillation destroys trust in the report permanently.

Implementation:

```python
if repo_scan.status != "ok":
    # carry every stored finding forward untouched
    for fp, rec in state.findings.items():
        results.append(rec | {"class": "UNCHANGED", "carried": True})
    report.not_scanned.append((repo, repo_scan.error))
    # do NOT update state.last_commit — next run must re-scan from the old point
    return
```

Every run's document must contain a **"Not scanned this run"** section listing each skipped repo
and why. A run that admits what it missed is fine. A run that reports `FIXED` for files it never
read is the bug.

### 2.5 Suppression

Each repo may contain a `.codepulse-ignore` file, `.gitignore` syntax, matching file paths; plus an
optional fingerprint list for silencing one specific finding:

```
# .codepulse-ignore
vendor/
legacy/reporting/*.py

# silenced findings (fingerprint  # reason)
a3f91c7d2e8b4105  # intentional: table is <500 rows, index not worth the write cost
```

Suppressed findings are counted but never detailed. One line in the document:
`7 findings suppressed by .codepulse-ignore.`

---

## 3. Fix 2 — the Evidence Rule

A static scan cannot prove a query is slow. It has not run the query. It does not know the table has
300 rows or 300 million. It has not read the migration that may already add the index. **One wrong
claim and the entire document gets ignored.**

The fix is not smarter analysis. It is honest labelling, enforced by the schema.

### 3.1 Two grades, required field

`grade` is a required enum on every finding: `CONFIRMED` | `SUSPECTED`. The model cannot omit it.
Validate with Pydantic strict mode; reject any finding missing it, the same way the Jira agents
validate LLM output against schema.

**CONFIRMED** — provable from the repository contents alone, needs no runtime knowledge.

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

**SUSPECTED** — depends on information the code does not contain.

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

### 3.2 Two rules that make the grade mean something

**Rule A — every `SUSPECTED` finding must carry a non-empty `how_to_verify` with a concrete command
or tool.** No verification step → the finding does not ship. This single rule eliminates
hand-waving, because the model cannot write a specific verification step for something it invented.

**Rule B — `SUSPECTED` findings never drive the headline.** The email subject counts confirmed only:

```
CodePulse 03 Oct 08:30 — 3 confirmed, 7 suspected, 5 fixed (31/40 repos)
```

An engineer who reads one CONFIRMED finding, checks it, and finds it correct will trust the next
one. That is the entire product.

### 3.3 Severity

`CRITICAL | HIGH | MEDIUM | LOW`, assigned by the check module, not by the model. A check module
that cannot justify its severity deterministically assigns `MEDIUM`.

---

## 4. Fix 3 — Scanning cost: four scan modes

The shape is: **first run scans everything; after that only what changed; once a week everything
again.** That is correct and it is what this section specifies. But "what changed" cannot mean
"the files in the diff" alone — see §4.2 — so the decision is expressed as four explicit modes,
chosen per repo, per run.

### 4.1 Mode selection

Evaluate in order. First match wins.

| # | Mode | Triggered when | What it reads |
|---|------|----------------|---------------|
| 1 | `FULL` | no state for this repo (**first run**) · weekly sweep · check-module set changed · state schema version bumped · previous run for this repo ended `error` or `budget_exhausted` | every eligible file |
| 2 | `REPO_FULL` | the diff touches any **context file** (§4.2) | every eligible file in that repo |
| 3 | `INCREMENTAL` | the diff touches only ordinary source files | the changed files, plus their **blast radius** (§4.3) |
| 4 | `SKIP` | `compare` returns zero changed files | nothing — one API call |

```python
def choose_mode(repo: str, state: RepoState | None, diff: Diff | None) -> Mode:
    if state is None:                                  return Mode.FULL   # first run
    if run.is_weekly_sweep:                            return Mode.FULL
    if state.checks_hash != current_checks_hash():     return Mode.FULL
    if state.schema_version != SCHEMA_VERSION:         return Mode.FULL
    if state.last_run_status != "ok":                  return Mode.FULL
    if diff is None or not diff.files:                 return Mode.SKIP
    if any(is_context_file(f) for f in diff.files):    return Mode.REPO_FULL
    return Mode.INCREMENTAL
```

The delta itself comes from one API call:

```
GET /repos/{org}/{repo}/compare/{last_commit}...{default_branch}
```

On a normal weekday most repos between 08:30 and 20:30 land in `SKIP`. That alone removes most of
the work.

### 4.2 Context files — why "changed files only" is not enough

**A change in one file can create or resolve a finding in a file that did not change.**

Three real cases:

- `migrations/0042_add_index.py` is added. It *fixes* the `missing-index` finding in
  `models/user.py`. But `models/user.py` is not in the diff, so an incremental scan never re-reads
  it, the finding never clears, and the report shows a resolved problem as open indefinitely.
- A service layer gains `prefetch_related("items__product")`. This resolves the `n-plus-one`
  flagged in a view file that did not change.
- An index is dropped in a migration. This *creates* a finding in a model file nobody touched.

So: if the diff touches a file that other findings depend on, the whole repo is re-scanned.

```python
CONTEXT_FILE_PATTERNS = (
    "**/migrations/**",            # schema — drives missing-index
    "**/schema.sql", "**/*.ddl",
    "**/models.py", "**/models/**", "**/entity/**",   # ORM definitions
    "requirements*.txt", "pyproject.toml", "poetry.lock",
    "package.json", "pom.xml", "build.gradle",        # dependency surface
    "**/settings.py", "**/application*.yml", "**/*.config.*",
    ".codepulse-ignore",
)
```

Keep this list in config, not in code — teams will need to extend it per stack.

Cost: schema and dependency files change rarely, so `REPO_FULL` fires a few times a week per repo,
not every run.

### 4.3 Blast radius within `INCREMENTAL`

Even for ordinary source files, read one hop out: for each changed file, also re-scan files in the
same package/module directory that import it. One hop, not transitive — transitive closure
degenerates to a full scan and costs more than it saves.

If import resolution is unavailable for a language, fall back to **same-directory** — cheap,
crude, and catches most of it.

### 4.4 The `FIXED` rule under incremental scanning

This is where an incremental scanner usually breaks, and §2.4 is what prevents it.

In `INCREMENTAL` mode the run reads maybe 6 files out of 800. The other 794 files' findings are
absent from the result set **because they were not looked at**, not because they were fixed. The
guard is already stated: `FIXED` requires `finding.path in run.files_read`. Everything else carries
forward as `UNCHANGED`.

Consequence to accept deliberately: **a fix in an unscanned file is reported as resolved at the
next `FULL` or `REPO_FULL` run for that repo, not immediately.** Worst case is the weekly sweep —
up to seven days late. That is the correct trade. Reporting a fix late is a minor annoyance;
reporting a fix that did not happen destroys the report's credibility. The context-file rule in
§4.2 means the common fixes (a migration, a dependency bump) clear on the very next run anyway.

Note this explicitly in the document footer so a reader is never confused:

```
Findings in files not read this run are carried forward unchanged.
Next full sweep: Sunday 05 Oct 08:30 IST.
```

### 4.5 Weekly full sweep

Sunday 03:00 UTC (08:30 IST): every repo in `FULL`, `last_commit` ignored, state rebuilt from
scratch. This is what catches drift — a check module added on Tuesday that never saw an unchanged
file, and any fix in a file that incremental runs skipped.

Budgets still apply. If the sweep cannot finish in one run, it persists its cursor and the Sunday
20:30 run continues it; the sweep is not marked complete in state until every repo has been
covered.

### 4.6 Hard budgets with a persisted cursor

Not "be efficient" — actual numbers the agent must respect.

```python
MAX_REPOS_PER_RUN  = 40
MAX_FILES_PER_REPO = 800
MAX_FILE_BYTES     = 400_000     # skip generated / minified blobs
MAX_RUN_SECONDS    = 900
```

On exhausting a budget the agent **stops and says so**, persists a cursor, and the next run resumes
from there, round-robin, so no repo is starved:

```
Budget: scanned 31 of 40 repos in 900s. Resuming from org/payments next run.
```

A truncated run that reports its truncation is acceptable. A truncated run that reports `FIXED` for
the repos it never reached is the failure from §2.4 — the guard covers this case too.

**A repo cut off by the budget is not marked scanned.** Its `last_commit` stays where it was and
its `last_run_status` is set to `budget_exhausted`, which forces `FULL` mode for it next run
(§4.1 rule 1). Without this, the repo's unscanned commits are silently skipped forever.

The first run is the expensive one by definition — every repo in `FULL`. Expect it to exhaust the
budget and span several runs. That is normal and the cursor handles it; the report just says so.

### 4.7 Skip before parsing, not after

```
vendor/  node_modules/  dist/  build/  target/  .venv/  __pycache__/
**/__generated__/**  **/migrations/*_auto_*.py
*.min.js  *.min.css  *.lock  *.svg  *.snap  *.map
```

Plus each repo's `.codepulse-ignore` (§2.5).

---

## 5. Check modules

Pluggable. Each module is a class implementing one interface, registered by `check_id`. Adding a
check must require zero changes to the scanner, the diff engine, or the report builder.

```python
class Check(Protocol):
    check_id: str                 # stable; part of the fingerprint — never rename
    languages: frozenset[str]
    default_grade: Literal["CONFIRMED", "SUSPECTED"]

    def run(self, file: SourceFile, ctx: RepoContext) -> list[Finding]: ...
```

`RepoContext` carries what a check needs beyond the single file — the set of migration files, the
dependency manifest, the ORM in use. It is what lets `missing-index` reach `CONFIRMED` instead of
`SUSPECTED`.

Ship these first:

| `check_id` | Default grade | Notes |
|---|---|---|
| `missing-index` | CONFIRMED when migrations are readable, else SUSPECTED | filtered/ordered column with no index |
| `n-plus-one` | SUSPECTED | ORM access inside a loop |
| `select-star` | CONFIRMED | `SELECT *` in raw SQL |
| `leading-wildcard-like` | CONFIRMED | `LIKE '%term%'` / `__icontains` — cannot use a b-tree index |
| `missing-pagination` | SUSPECTED | list endpoint with no limit/offset |
| `swallowed-exception` | CONFIRMED | `except: pass`, empty `catch {}` |
| `hardcoded-secret` | CONFIRMED | report file + line + fact only, **never the value** |

A check that cannot decide returns nothing. Silence beats a false positive.

---

## 6. Output

### 6.1 Document

Reuse `report.py:83 build_docx_report()`. Structure:

1. **Header** — org, run timestamp (IST and UTC), repos scanned / total, run duration, and the
   mode breakdown: `FULL 2 · REPO_FULL 3 · INCREMENTAL 9 · SKIP 26`.
2. **Summary** — counts by class and grade; the one-line `UNCHANGED` roll-up.
3. **NEW findings** — full detail, CONFIRMED before SUSPECTED, severity descending.
4. **WORSE findings** — with the previous severity shown.
5. **FIXED findings** — one line each (repo · path · check · first seen).
6. **Not scanned this run** — repo and reason, per §2.4. Omit the section only when empty.
7. **Appendix A — unchanged findings** — one line each.
8. **Appendix B — suppressed** — count only.
9. **Footer** — the §4.4 note: findings in files not read this run are carried forward, and the
   date of the next full sweep.

Every finding renders: grade, severity, check id, repo, path, the evidence, the fix (CONFIRMED) or
the `how_to_verify` (SUSPECTED), and `first_seen`.

### 6.2 Email

Reuse `shared/mailer.py` including its repeat-suppression behaviour.

Subject: `CodePulse {DD Mon HH:MM IST} — {n} confirmed, {n} suspected, {n} fixed ({scanned}/{total} repos)`

Body: the summary block, the NEW findings as plain text, and nothing else. The document is attached.

**A quiet run still sends an email.** Silence is ambiguous — the reader cannot tell "nothing found"
from "the agent is dead."

```
CodePulse 03 Oct 08:30 — 0 confirmed, 0 suspected, 2 fixed (40/40 repos)

No new findings. Two findings from the previous run are resolved:
  org/billing-api  models/user.py  missing-index   (open since 28 Sep)
  org/auth         views.py        swallowed-exc   (open since 01 Oct)

34 findings carried over unchanged. Full detail attached.
```

**A failed run must also email.** If the run cannot start at all — GitHub auth failure, no repos
reachable — send a short failure notice with the error. Never fail silently; that is the same hole
as `jira-orchestration/src/tools/tools.py:198` (D0), where an exception was swallowed into a
"proceed: false" that looked identical to a legitimate drop and the user got nothing.

---

## 7. Configuration

| Variable | Meaning |
|---|---|
| `CODEPULSE_GITHUB_ORG` | organisation to scan |
| `CODEPULSE_GITHUB_TOKEN` | read-only PAT; `repo:read` scope, nothing more |
| `CODEPULSE_RECIPIENTS` | comma-separated |
| `CODEPULSE_TIMEZONE` | default `Asia/Kolkata` |
| `CODEPULSE_MAX_REPOS` / `_MAX_FILES` / `_MAX_RUN_SECONDS` | budgets, §4.3 |
| `CODEPULSE_REPO_ALLOWLIST` / `_DENYLIST` | optional |
| `CODEPULSE_DRY_RUN` | scan and build the document, send no email |

All times stored in state are UTC. Only the display layer converts to IST. Do not store local time.

---

## 8. Acceptance criteria

The implementation is complete when all of these hold:

1. Adding twelve blank lines above a flagged statement produces `UNCHANGED`, not `FIXED` + `NEW`.
2. A repo that fails to clone appears under "Not scanned this run", **none** of its findings is
   marked `FIXED`, and its `last_commit` in state is unchanged.
3. Every `SUSPECTED` finding in the document has a non-empty, specific `how_to_verify`. A finding
   without one is rejected at validation, not rendered.
4. The email subject's confirmed count equals the number of `CONFIRMED` findings and is unaffected
   by `SUSPECTED` ones.
5. Two consecutive runs with no commits in between: the second sends an email reporting zero new
   findings and makes at most one API call per repo (all repos in `SKIP`).
6. Hitting `MAX_RUN_SECONDS` produces a report naming the resume point, the cut-off repo's
   `last_commit` is unchanged, its status is `budget_exhausted`, and it runs in `FULL` mode next.
6a. **First run on a repo with no state selects `FULL`**, regardless of what `compare` would return.
6b. **A commit that only adds a migration file selects `REPO_FULL`, not `INCREMENTAL`**, and a
   missing-index finding in an otherwise-unchanged model file is correctly marked `FIXED` by it.
6c. A commit touching one ordinary source file selects `INCREMENTAL`, and **no finding in any
   unread file is marked `FIXED`** — all carry forward as `UNCHANGED`.
6d. Changing the enabled check-module set forces `FULL` on every repo at the next run.
7. A finding listed in `.codepulse-ignore` by fingerprint is counted in the suppressed total and
   appears nowhere else.
8. Searching the built artifact for the GitHub token and the SMTP password finds neither.
9. A run that cannot start at all still emails a failure notice.
10. Renaming a check module's `check_id` is caught by a test, because it would invalidate every
    stored fingerprint for that check.

---

## 9. Build order

1. State store + fingerprinting + the diff engine, with the §2.4 guard. Tests first.
2. GitHub client: list repos, compare commits, fetch file contents. Mode selection (§4.1),
   context files (§4.2), budgets and skip-list.
3. One check module (`leading-wildcard-like` — smallest, unambiguous, CONFIRMED).
4. Report builder + mailer, including the quiet-run and failed-run paths.
5. Schedule registration (two cron triggers + the Sunday sweep).
6. The remaining check modules.

Steps 1 and 4 are where the value is. Step 6 is where the temptation is. Do not reorder.
