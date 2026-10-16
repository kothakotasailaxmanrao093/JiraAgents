# From a Jira story to a merged pull request

Every step is a comment. The Orchestration agent reads each one, sends it to the
right agent, passes on what the next step needs, and replies in the same place.
Nothing is emailed.

| Where | Comment | What happens |
|---|---|---|
| Jira story | `@Aetherion help` | the list below |
| Jira story | `@Aetherion review` | **Requirement Review** reads the description, comments and attachments; replies with findings, gaps and questions, and names any attachment it could not read |
| Jira story | *(answer in a comment)*, then `@Aetherion review` | the review reads the answers and lists only what is still open |
| Jira story / epic | `@Aetherion build` | **Task Creation**: a Story gets Sub-tasks, an Epic gets Stories; the card itself is never changed; the reply links every new issue |
| Jira story | `@Aetherion explain this card` | what the ticket asks for |
| Jira story | `@Aetherion status` | what is running on the ticket now and every step so far (review, build, TDD, code, merge), with times and run ids — no agent is called |
| Jira story | `@Aetherion create TDD` | a readiness review first (4/5 needed; otherwise the questions are posted); then **Planning** writes the TDD from the story, its sub-tasks and the code graph; it is published in the project's **Confluence** space and linked on the story |
| TDD page | `@Aetherion explain why this approach was selected` | **Planning** answers under the comment, from the TDD, the discussion and the code |
| TDD page | `@Aetherion why can't we use <alternative>?` | Planning weighs the alternative; it suggests a change when one is warranted |
| TDD page | `@Aetherion update the TDD[: what to change]` | Planning revises the design; the page gets a new version |
| TDD page | `@Aetherion code` | proposes the branches (source, new branch, target) and waits |
| TDD page | `@Aetherion code confirm` or `@Aetherion code source=main branch=feature/X target=main` | **Coding** implements the TDD, runs the repository's tests, opens a pull request — linked on the page and on the story. If it cannot implement the design, or the PR's tests fail, the reasons go back to Planning, the TDD is revised and Coding tries again (the failed PR is closed) |
| TDD page | `@Aetherion code confirm bank.target=release/2.1 ledger.source=develop` | the same, with branches for one repository only (`<repo>.source=`, `<repo>.target=`; the new branch name is shared); the others keep the proposal. When a design touches several repositories, the reply lists the order to **merge and deploy** them (providers before consumers) and each PR links the others |
| GitHub | a person merges the PR | the GitHub webhook gives the merge to **Planning**, which refreshes its code graph; the story and the TDD page are told |

`explain why`, `update the TDD` and `code` also work on the Jira story (they use the story's TDD).

## Run it on a laptop

Needs: the five agent folders with `.venv` (`uv sync` in each), Docker running
`planning-neo4j` and `planning-s3`, and `.env.local` (git-ignored) with:

```bash
OPENAI_API_KEY=…                       # laptop only: the agents call OpenAI directly
GITHUB_TOKEN=…                         # Contents + Pull requests: read and write on the repos
ORCH_PLANNING_REPOS="bank: AetherionAgentDemo/bank-transfer-service"
ORCH_BUILD_PIPELINE=                   # build only; TDD and code are their own commands
```

```bash
scripts/run_local.sh --poll --follow --keyword AetherionLocal
```

- `--poll` listens for new Jira comments every 20 s (on Aetherion the Jira webhook does
  this). Confluence comments and GitHub events arrive **by webhook only**: a webhook
  cannot reach a laptop, so test TDD-page comments and merges on Aetherion.
- `--keyword AetherionLocal`: the laptop answers only `@AetherionLocal …`, so the
  published router (same Jira webhook) never answers your tests.
- Logs: `.local-logs/<agent>.log`, `.local-logs/jira-watch.log`.

## Test it through Jira, Confluence and GitHub

1. In Jira, create a Story with a description and attachments.
2. Comment `@AetherionLocal help`, then `@AetherionLocal review`. Answer the questions in a
   comment and review again.
3. `@AetherionLocal build` — Sub-tasks appear under the Story, linked in the reply.
4. `@AetherionLocal create TDD` — the reply links the Confluence page.
5. On the page: `@AetherionLocal explain why…`, `@AetherionLocal why can't we use…?`,
   `@AetherionLocal update the TDD: …`, then `@AetherionLocal code` and `@AetherionLocal code confirm`.
6. In GitHub, review the PR; merge it only when its **Verification** says the tests passed.
   Within a minute the story and the page say the code graph was refreshed.

Another Jira site (a friend's): put its settings in `.env.site.NAME` (template
`.env.site.example`) and add `--site NAME`.

## Publish

Nothing in the code changes between the laptop and Aetherion; only settings do.

1. **Orchestration** (`jira-orchestration/.env`): `JIRA_*`, `ORCH_ALLOWED_PROJECT_KEYS`,
   `ORCH_NOTIFY_ON=none`, `ORCH_PLANNING_REPOS`, `GITHUB_TOKEN` and `ORCH_GITHUB_ORG` (to watch
   merges), the child queues `ORCH_TASK_QUEUE_JIRA_TASK_CREATION`,
   `ORCH_TASK_QUEUE_JIRA_REQUIREMENT_REVIEW`, `ORCH_TASK_QUEUE_PLANNING`, `ORCH_TASK_QUEUE_CODING`.
   Optional: `ORCH_CONFLUENCE_SPACES` (e.g. `BGV:PR`), `ORCH_TDD_MIN_READINESS`.
2. **Task Creation** (`.env`): `TASK_NOTIFY_ON=none`, `GENERATE_LOCAL_PDF=false`.
3. **Planning and Coding** (their own `.env`): `NEO4J_URI/USER/PASSWORD` of a Neo4j the platform
   can reach (the laptop's Docker is not), `GITHUB_TOKEN` with write access for Coding.
   Their code changes (graph-only refresh, design Q&A, TDD as Confluence content, confirmed
   branches, no retry of the coding tool, closing a superseded PR) live in Suhani's repos and
   must be pushed there and published from there.
4. **Webhooks**, no Schedules: Jira, Confluence and GitHub push every event (see
   "Webhooks" below). Polling was removed on 2026-10-16; a Schedule left on gets
   `"status": "ignored"` with "Switch this Aetherion Schedule off" and does nothing.
5. Publish with `scripts/publish.py`; then run `{"health_check": true}` on Orchestration.

### What makes it safe in production

- **Tests.** Coding runs the repository's tests (`mvn test` for Java, at the repository's own
  Java version). Where the build tool is missing (the Aetherion container has no Maven), the
  repository's **CI** decides: Coding waits up to `CODING_CI_WAIT_MINUTES` (default 10) for
  the checks on its commit. CI passed → the draft PR is marked ready; CI failed → the failures
  go back to Planning like a failed local test; no CI → the PR stays a draft that says
  "tests not run". Run `mvn test` yourself before merging such a PR.
- **Existing behaviour.** Planning reads the build files (Maven, Gradle, version catalogs,
  Spring Boot's managed versions) and the API contracts (OpenAPI, AsyncAPI, proto) and writes
  them into the TDD; Coding refuses a change that removes a method, an endpoint, or makes an
  optional input required, unless the design asks for it.
- **Secrets.** Every token and password is read first from a mounted file
  (`<NAME>_FILE=/path`, or a file named `<NAME>` in `SECRETS_DIR`, `/run/secrets` or
  `/etc/secrets`), then from the environment. `scripts/publish.py` warns when a `.env` that
  would be published holds a secret; `--no-secrets-in-env` stops the publish instead.
- **Identity.** Orchestration calls each agent only on its own task queue
  (`ORCH_TASK_QUEUE_*`); with a queue missing it refuses, and the health check names it.
- **Alerts.** When Coding gives up, `ORCH_ALERT_WEBHOOK_URL` (a Slack/Teams/Google Chat
  incoming webhook) gets the reason, even with email off. `ORCH_CODING_TIMEOUT_MINUTES`
  (default 35) covers Coding's CI wait.

## Webhooks (the only way events arrive)

Every event reaches the Orchestration agent through the platform's webhook service
(`unified-webhook` in the platform's `antennae` repo; route `/webhooks/v2/<provider>`),
which puts it on the tenant's queue; the platform starts the subscribed agent and fills
its input by **field name**. Nothing is polled.

| Event | Sent by | Orchestration input | Goes to |
|---|---|---|---|
| `@Aetherion …` comment on a Jira story | Jira webhook (as today) | `issue_key`, `comment_id`, `webhookEvent` | the command's agent; reply on the story |
| `@Aetherion …` comment on a TDD page | Confluence Automation rule | `confluence_page_id`, `confluence_comment_id` | the same command flow; reply under the comment |
| A pull request merged into `main` | GitHub webhook | `github_event`, `github_action`, `github_repo`, `pr_number` | Planning (graph refresh); a note on the story and page |
| A comment or review on a pull request | GitHub webhook | the same, plus `github_comment_id` | Planning |

Each comment is marked answered, and each GitHub event recorded as sent, before the work
starts, so an event delivered twice is never answered or sent twice.

### 1. The webhook service (deployed by the platform team)

- `providers/confluence.py` (new): accepts the rule's request when its
  `X-Confluence-Webhook-Secret` header equals `CONFLUENCE_WEBHOOK_SECRET`; tenant from
  `?tenant_id=`; emits `page.commented` with `confluence_page_id`, `confluence_comment_id`,
  `confluence_page_title`, `confluence_space_key`, `confluence_author`.
- `providers/github.py`: the tenant may also come from `?tenant_id=` (GitHub cannot send the
  `X-Tenant-ID` header); emits flat `github_event`, `github_action`, `github_repo`,
  `pr_number`, `pr_merged`, `pr_base`, `github_comment_id`, `github_comment_kind`.
- `webhook_handler.py`: `confluence` registered, its secret read from the tenant secret.
- Tenant secret: add `CONFLUENCE_WEBHOOK_SECRET` and `GITHUB_WEBHOOK_SECRET`.
- Subscribe **JiraOrchestration** to: `confluence` `page.commented`; `github`
  `pull_request.merged`, `issue_comment.created`, `pull_request_review_comment.created`,
  `pull_request_review.submitted` — mapping fields by name, as for Jira.

### 2. The Confluence Automation rule (once, in Confluence → Automation)

- **When:** a comment is added.
- **If:** the page has the label `aetherion-tdd`; the comment contains `@Aetherion`; it does
  not contain `AetherionAgent ·` (the agent's own replies).
- **Then: Send web request**
  - URL: `https://wh.sbox.aetherion.io/webhooks/v2/confluence?tenant_id=<tenant id>&tenant=test`
    (the same `tenant_id` as the Jira webhook's URL)
  - Method `POST`; headers `Content-Type: application/json` and
    `X-Confluence-Webhook-Secret: <CONFLUENCE_WEBHOOK_SECRET>`
  - Body (custom data) — check each smart value's exact name in the rule editor:
    ```json
    {
      "event": "comment_created",
      "page_id": "{{page.id}}",
      "comment_id": "{{comment.id}}",
      "page_title": "{{page.title}}",
      "space_key": "{{space.key}}",
      "author_id": "{{comment.author.accountId}}",
      "author_name": "{{comment.author.displayName}}"
    }
    ```

### 3. The GitHub webhook (repository or organization → Settings → Webhooks → Add)

- Payload URL: `https://wh.sbox.aetherion.io/webhooks/v2/github?tenant_id=<tenant id>&tenant=test`
- Content type `application/json`; Secret: `GITHUB_WEBHOOK_SECRET`.
- Events: **Pull requests**, **Issue comments**, **Pull request review comments**,
  **Pull request reviews**. Only repositories Orchestration watches (`ORCH_GITHUB_ORG`,
  `ORCH_PLANNING_REPOS`) are acted on; a merge counts only into `ORCH_GITHUB_BRANCH` (`main`).

### The URLs, from settings

Set in `jira-orchestration/.env` once the platform team gives the address:
`ORCH_WEBHOOK_BASE_URL` (e.g. `https://wh.sbox.aetherion.io/webhooks/v2`),
`ORCH_WEBHOOK_TENANT_ID`, `ORCH_WEBHOOK_TENANT`. `{"health_check": true}` then prints the
Jira, Confluence and GitHub URLs to configure — until then it lists the checks that wait
for them (the Confluence rule and the GitHub webhook; Jira already works).

### 4. Check it

Before the platform side is ready, each mode can be run by hand from the Orchestration
form (or `start_workflow` on its queue) with the input the webhook would send:

```json
{"confluence_page_id": "16908289", "confluence_comment_id": "16700001"}
{"github_event": "pull_request", "github_action": "closed",
 "github_repo": "AetherionAgentDemo/bank-transfer-service", "pr_number": 21}
{"github_event": "issue_comment", "github_action": "created",
 "github_repo": "AetherionAgentDemo/bank-transfer-service", "pr_number": 21,
 "github_comment_id": "901"}
```

The result says what was done, or why nothing was (`skipped`: "not a TDD page", "already
answered", "pull request opened is not a merge", …). Delete any old Aetherion Schedule
for `github_poll` or `confluence_poll`: polling was removed on 2026-10-16.
