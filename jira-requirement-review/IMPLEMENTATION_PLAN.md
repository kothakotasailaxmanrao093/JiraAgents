# Implementation Plan — Jira Requirement Review Agent (v1, historical)

> ⚠️ **Historical record. Do not follow this to run or change the agent.**
>
> The project was scaffolded as `demo_pranil`, and that name appears throughout
> this document. It is now **`jira-requirement-review`**, registered with the
> platform as `JiraRequirementReview`. Other names here are equally stale.
>
> For anything current, read [README.md](README.md), and
> [../PLAN.md](../PLAN.md) for how this agent fits the routed system.
>
> Status: **Implemented (v1), then extended (v2).** This document is the original v1 design and is
> kept as historical record — several v1 details below (e.g. the `llm/` `LLMPort`/`LlmFactoryAdapter`
> abstraction) were superseded during implementation by routing LLM calls through the Aetherion AI
> Gateway directly (`src/tools/analyze_requirement_tool.py`). v2 merged in the pre-grooming clarity
> agent's batch/JQL review, linked-issues context, three more finding categories, priority tagging,
> per-issue readiness scoring, and Word-report/email delivery — see [README.md](README.md) for the
> current, accurate feature set and usage.

## Context

The team wants an Aetherion agent that reviews a Jira requirement across multiple sources and
helps a human post a clarification comment. It reads the **target issue (description + comments)**,
its **parent card (description + comments)**, **all subtasks of the target (description + comments)**,
and an optional **requirements-meeting transcript**, then uses an LLM to surface **Assumptions,
Open Questions, Requirement Gaps, Ambiguities, Missing Functional Details, Missing Technical
Details**. The draft is **always reviewed by a human before it reaches Jira**; posting is a
separate, explicit step. Built as a layered, testable, extensible foundation (more context
sources later: Confluence, Slack), **not** a copy of `asurint/my_first_agent`'s flat style.

Target project: `/Users/Pranil.Ghatage/Desktop/atherion/demo_pranil` (clean hello-world scaffold —
no Jira/LLM code; no migration concerns).

### Locked decisions
1. **Two-step / two agents** — `JiraRequirementReview` generates & returns the draft (NEVER
   posts); `PostJiraReviewComment` posts the human-approved/edited comment in a separate run.
2. **Context** = target (desc + comments) + **parent** (desc + comments) + **all subtasks** of the
   target (desc + comments each) + optional transcript.
3. **Transcript = file upload**, bucket-gated, and **multi-card aware** (the LLM uses only the parts
   relevant to the target issue and ignores unrelated discussion).
4. **LLM = `agent_lib` `LlmFactory`** (multi-provider) behind an internal `LLMPort`.
5. **No `OPENAI_API_KEY` in project config** — provided by the platform runtime (as in
   `my_first_agent`); we set only the model id.
6. **Storage is optional & bucket-gated** — no bucket (`team_id`/`TENANT_ID`) ⇒ transcript skipped
   and nothing stored; if a bucket is present, use only that bucket. No mandatory S3 artifact.

## Current-State Analysis (`demo_pranil`)
- Fresh scaffold: `src/agent/agent.py` (`@agent() demo_pranil` hello-world), `src/tools/tools.py`
  (greet/analyze/enrich), two `metadata.json`, `src/__init__.py` exporting `demo_pranil`.
  No `utils/`, no `tests/`.
- `pyproject.toml` deps = only `aetherion-sdk`. `.env` = only `AETHERION_*_TASK_QUEUE`.
- Python `>=3.12.10,<3.14`; pytest/pytest-asyncio/ruff/black in dev group.

## Architecture — Layered ports & adapters (within SDK agent+tools seams)

SDK seams: `@agent` (deterministic, **no I/O**) and `@tool` (Temporal activity, **all I/O**).
Business logic lives in a framework-free internal package; tools are thin shims.

```
@agent orchestrators ─toolExecutor.execute─▶ thin @tool shims ─plain calls─▶ internal pkg
(2 agents: review / post)                    (~15 lines: env→adapter→dict)   (jira/ llm/ context/ review/)
```

Why: pure logic (ADF render, markdown→ADF, dedup/grouping, prompt build, finding models) becomes
unit-testable with no network; Jira/LLM are swappable adapters behind ports; new context sources
are additive via a `ContextSource` ABC.

### Extensibility seam — `ContextSource`
Each source yields `ContextDocument(source_id, source_label, kind, text, metadata)`. The LLM cites
`source_label` verbatim as **Evidence Source**. Per issue we emit *separate* docs for description
vs comments (precise evidence), e.g. `Target ABC-123 (description)`, `Target ABC-123 (comments)`,
`Parent ABC-100 (description)`, `Subtask ABC-124 (comments)`, `Meeting Transcript`. Adding
Confluence/Slack later = new `load()` + label, no orchestrator/prompt edits. Sources do async I/O ⇒
only ever run inside the `gather_context` activity, never in the workflow.

## File Tree

```
demo_pranil/
├── pyproject.toml                      # MODIFY: add aiohttp, pydantic, python-dotenv
├── .env / .env.template                # MODIFY: add JIRA_*, OPENAI_MODEL_ID; TENANT_ID optional
├── src/
│   ├── __init__.py                     # REPLACE: export both agents
│   ├── config.py                       # NEW: Settings.from_env(), mask(), team_id resolution, caps
│   ├── agent/
│   │   ├── review_agent.py             # NEW @agent JiraRequirementReview (draft only)
│   │   ├── post_agent.py               # NEW @agent PostJiraReviewComment (posts approved comment)
│   │   ├── metadata.json               # REPLACE: triggers for BOTH agents (see Config)
│   │   └── __init__.py
│   ├── tools/
│   │   ├── tools.py                    # REPLACE: aggregator importing the 4 *_tool modules
│   │   ├── metadata.json               # keep (config: {})
│   │   ├── gather_context_tool.py      # NEW @tool gather_context
│   │   ├── analyze_requirement_tool.py # NEW @tool analyze_requirement
│   │   ├── render_review_tool.py       # NEW @tool render_review (pure: ADF + markdown preview)
│   │   └── post_review_tool.py         # NEW @tool post_review_comment (markdown/findings→ADF + POST)
│   ├── jira/
│   │   ├── http.py                     # NEW: base_url/auth/mask/preflight/get_json/post_json/adf_to_text
│   │   └── client.py                   # NEW: JiraClient (get_issue, get_comments, get_parent, get_subtasks, post_comment)
│   ├── llm/
│   │   ├── port.py                     # NEW: LLMPort.complete_json + tolerant _extract_json
│   │   └── llm_factory_adapter.py      # NEW: LlmFactoryAdapter (agent_lib LlmFactory)
│   ├── context/
│   │   ├── base.py                     # NEW: ContextDocument, ContextSource ABC, FetchContext
│   │   ├── jira_sources.py             # NEW: TargetIssueSource, ParentIssueSource, SubtasksSource
│   │   ├── transcript_source.py        # NEW: TranscriptSource (storage read, bucket-gated)
│   │   └── registry.py                 # NEW: build_sources(ctx)
│   └── review/
│       ├── models.py                   # NEW: Finding/FindingType/Confidence/Review (Pydantic v2)
│       ├── prompt.py                   # NEW: SYSTEM_PROMPT, build_user_message, char caps
│       ├── parser.py                   # NEW: parse_findings() -> Review (validate/coerce/drop bad)
│       ├── dedup.py                    # NEW: dedup + group_by_category (PURE)
│       └── adf.py                      # NEW: AdfBuilder, render_review_to_adf, markdown_to_adf (PURE)
└── tests/                              # NEW
    ├── conftest.py
    ├── test_adf.py  test_markdown_to_adf.py  test_dedup.py  test_models.py  test_prompt.py
    ├── test_context_registry.py  test_jira_client.py  test_review_flow.py  test_post_flow.py
```
Scaffold tools and hello-world agent are removed/replaced.

## Key Signatures

**Agent A — review/draft** (`src/agent/review_agent.py`)
```python
@agent(name="JiraRequirementReview")
async def JiraRequirementReview(payload: dict) -> list[dict]:
    # payload: issue_key(str,req), include_parent(bool=True), include_subtasks(bool=True),
    #          uploaded_files(str|None transcript file key), team_id(str|None), model_id(str|None)
    # Steps: gather_context(2m) -> [early exit if no requirement text] ->
    #        analyze_requirement(5m) -> render_review(30s) -> return draft (NOT posted)
    # Returns list[dict]: {status, issue_key, finding_count, by_category,
    #   "comment_markdown": <editable draft>, "comment_adf": {...}, "findings":[...],
    #   "message":"Draft only — review, then post via PostJiraReviewComment."}
```

**Agent B — post** (`src/agent/post_agent.py`)
```python
@agent(name="PostJiraReviewComment")
async def PostJiraReviewComment(payload: dict) -> list[dict]:
    # payload: issue_key(str,req),
    #          comment_markdown(str|None approved/edited text)  OR  findings(list|None),
    # Steps: post_review_comment(1m) -> return result
    # Returns list[dict]: {status, issue_key, comment_id, comment_url, message}
```

**Tools**
```python
@tool(name="gather_context")
async def gather_context(issue_key, include_parent=True, include_subtasks=True,
                         transcript_file_key=None, team_id=None) -> dict
    # -> {documents:[...], has_requirement:bool, counts:{parent,subtasks,comments},
    #     warnings:[...], error:str|None}

@tool(name="analyze_requirement")
async def analyze_requirement(documents:list[dict], issue_key, issue_summary, model_id=None) -> dict
    # -> {findings:[{finding_type,description,evidence_source,confidence}...], error:str|None}  (never raises)

@tool(name="render_review")                          # PURE — no network, no post
async def render_review(issue_key, findings:list[dict]) -> dict
    # -> {comment_markdown:str, comment_adf:{...}, by_category:{...}}

@tool(name="post_review_comment")                    # the only Jira write
async def post_review_comment(issue_key, comment_markdown=None,
                              findings=None, footer:str="") -> dict
    # markdown_to_adf(comment_markdown) OR render_review_to_adf(findings); append footer; POST
    # -> {posted:bool, comment_id:str|None, comment_url:str|None, error:str|None}
```

**Internal**
```python
class JiraClient:                              # src/jira/client.py
    async def preflight(self) -> None
    async def get_issue(self, key, fields) -> dict          # GET /rest/api/3/issue/{key}
    async def get_issue_bundle(self, key) -> dict           # {key,summary,description_text,
                                                            #  comments:[...], parent_key, subtask_keys:[...]}
    async def get_parent(self, parent_key) -> dict | None
    async def get_subtasks(self, subtask_keys, cap) -> list[dict]
    async def post_comment(self, key, adf_doc) -> dict      # POST .../issue/{key}/comment -> {id,...}

class LLMPort(Protocol):
    async def complete_json(self, system:str, user:str, model_id:str|None=None) -> dict

# src/review/adf.py
def render_review_to_adf(review: Review, issue_key:str, footer:str) -> dict   # structured path (pure)
def markdown_to_adf(md:str, footer:str="") -> dict                            # human-edited path (pure)
```

## Jira Integration Approach
- Reuse/generalize from `asurint/my_first_agent/src/tools/jira_fetch_dashboard_issues.py` into
  `src/jira/http.py`: `base_url()`, `auth()` (BasicAuth), `mask()`, `preflight_auth()`
  (GET `/myself`), `get_json()`, and `adf_to_text()` (ADF→text).
- **New:** single-issue `GET /rest/api/3/issue/{key}?fields=summary,description,parent,subtasks,comment`
  (dashboard tool's default fields omit `description`/`parent`/`subtasks`); `comment` field gives
  `{comments:[{author,created,body(ADF)}]}` → flatten with `adf_to_text`, capped. Subtasks come as
  `fields.subtasks=[{key,...}]` (summaries only) → fetch each subtask issue for its description +
  comments. `post_json()` (raises on non-2xx, truncated body). `post_comment` → `POST
  /rest/api/3/issue/{key}/comment` with `{"body": <ADF>}`.
- **ADF writing** (`src/review/adf.py`): `render_review_to_adf` (sections only for non-empty
  categories; each bullet = bold `<Type> · <confidence>` — description + `Evidence: <label>`) and
  `markdown_to_adf` (subset: `##/###` headings, `-` bullets, `**bold**`, paragraphs) for the
  human-edited draft. Footer line: "Generated by Requirement Review Agent · <UTC ts> · wf <id>".

## Context & Transcript Approach
- `gather_context` fetches the target bundle once, then runs sources: `TargetIssueSource`
  (desc + comments), `ParentIssueSource` (parent_key → desc + comments; `[]` if none),
  `SubtasksSource` (each subtask key, capped → desc + comments), `TranscriptSource`.
- **Transcript (bucket-gated):** `team_id = payload.team_id or TENANT_ID`; if absent → skip with a
  warning (degrade). If present → `storage.init_client()` + `storage.retrieve(team_id, file_key,
  RetrievalMode.FULL_OBJECT)`. Decode text for `.txt/.vtt/.md/.csv`; for `.docx/.pdf` use
  `agent_lib.utils.file_utils.fetch_reference_data`. Hard char cap before returning.
- **Multi-card transcript:** prompt instructs the model to use only transcript segments relevant to
  the target issue (match by key/summary/topic), ignore unrelated tickets, and never attribute other
  tickets' decisions to this one.
- **Caps** (in `config.py`): MAX_SUBTASKS (~15), MAX_COMMENTS_PER_ISSUE (~20),
  MAX_CHARS_PER_COMMENT (~1000), MAX_DESC_CHARS (~8000), MAX_TRANSCRIPT_CHARS (~24000) — protect the
  ~500KB Temporal payload limit and token cost.

## LLM & Prompt Strategy (`agent_lib` LlmFactory)
- `LlmFactoryAdapter.complete_json`: `LlmFactory.detect_provider(model_id)` → `build_config`/config
  with platform-provided key → `get_llm_connection(...)` → `call_llm_sync(references=[], prompt=...)`
  (combine SYSTEM+USER into one prompt). No native JSON mode ⇒ prompt demands "ONLY JSON, no fences"
  and `port._extract_json` strips ```json fences / isolates outermost `{...}` before `json.loads`;
  tolerant of chunk iterator vs single message.
- **System prompt** (`src/review/prompt.py`): senior-BA role; reason ACROSS labeled sources
  (descriptions + comments from target/parent/subtasks + relevant transcript); distinguish **stated
  facts vs inferred gaps**; only emit assumptions the requirement implicitly relies on (no
  fabrication); **dedup** overlapping findings; confidence semantics (high=textual, medium=inference,
  low=speculative—prefer omitting noise); empty categories valid; strict JSON
  `{"findings":[{finding_type, description, evidence_source, confidence}]}` with enums matching
  `FindingType`/`Confidence`.
- **User message**: each `ContextDocument` as `[SOURCE: <label>] (kind: <kind>)` + capped text; the
  bracket label is the exact `evidence_source` string the model must cite.

## Testing Strategy
- Pure no-network units (bulk): `test_adf` (doc shape, only non-empty sections, round-trip via
  `adf_to_text`), `test_markdown_to_adf` (headings/bullets/bold/paragraph mapping; footer),
  `test_dedup` (collapse, grouping, omission, stable order), `test_models`/parser (reject empty,
  coerce casing, drop invalid finding_type), `test_prompt` (one `[SOURCE:]` per doc, truncation
  marker, transcript-relevance instruction present), `test_context_registry` (parent only when
  include_parent; subtasks only when include_subtasks; transcript only with file key + bucket).
- DI seams: `FakeLLM(LLMPort)` returns canned (incl. malformed/duplicate) JSON; `JiraClient` takes
  an injected aiohttp session faked in `test_jira_client` (asserts `description,parent,subtasks,
  comment` requested; comments flattened/capped; subtasks fetched up to cap; `get_parent` None when
  absent; `post_comment` POSTs `{"body": <adf>}`). `conftest.py` sets `JIRA_*`/`OPENAI_MODEL_ID`/
  `TENANT_ID` via `monkeypatch.setenv`.
- Flow tests: `test_review_flow` (monkeypatch `toolExecutor.execute`; assert draft `list[dict]` for
  happy / no-requirement-fatal / LLM-error-degrade; **assert never posts**). `test_post_flow`
  (markdown→ADF post happy path; post-failure degrade; missing-input error).

## Error-Handling Strategy
| Condition | Class | Behavior |
|---|---|---|
| Missing `JIRA_*` / target 404 / no description | **fatal** | stop; `status:"error"` naming the issue |
| Parent missing / parent fetch fails | degrade | skip parent + warning |
| Subtask fetch fails (some) | degrade | skip that subtask + warning; keep the rest |
| Transcript: no bucket / not provided / read fails | degrade | skip transcript (+warning if key given) |
| LLM key missing / call fails / bad JSON | degrade | `{findings:[], error}` (never raise); review agent returns `completed_with_warnings`, no draft fabricated |
| Some findings invalid | degrade | parser drops bad rows, logs count |
| Post fails (Agent B) | degrade | `posted:False`+error returned; `completed_with_warnings` |
| Agent B missing both comment_markdown & findings | **fatal** | `status:"error"` (nothing to post) |

Logging via `common_lib.utils.logger.setup_logger`: one INFO per tool (issue_key, source/finding
counts); `preflight()` logs authenticated identity before any write; secrets via `mask()`;
`exc_info=True` on caught errors; post logs `comment_id` + issue URL; agents log
`workflow.info().workflow_id`.

## Config
**Env (.env / .env.template additions):**
```
JIRA_BASE_URL=https://<site>.atlassian.net
JIRA_EMAIL=<user>
JIRA_API_TOKEN=<token>
OPENAI_MODEL_ID=gpt-4o      # model id only (provider auto-detected). API key is provided by the
                            # platform runtime — NOT committed here (same as my_first_agent).
# TENANT_ID=<team/bucket>   # OPTIONAL. Only needed for transcript file reads. If unset, the agent
                            # skips the transcript and stores nothing. If set, only this bucket is used.
# keep existing AETHERION_AGENT_TASK_QUEUE / AETHERION_TOOL_TASK_QUEUE
```
**pyproject deps add:** `aiohttp==3.13.5`, `pydantic>=2.7`, `python-dotenv>=1.0.1`
(`agent_lib`/`common_lib` are bundled with the `aetherion-sdk` wheel; **no** `openai`/`python-docx`).

**`src/agent/metadata.json` triggers** (one file describing both agents' inputs):
- `JiraRequirementReview`: `issue_key`(str,req), `include_parent`(bool), `include_subtasks`(bool),
  `transcript_file`(file, optional → `uploaded_files` key).
- `PostJiraReviewComment`: `issue_key`(str,req), `comment_markdown`(textarea, req — the reviewed text).
(Confirm at implementation how the SDK maps a `file` trigger and multiple agents in one metadata
file; fall back to `transcript_file_key` if `uploaded_files` is not the surfaced key.)

## Risks & Assumptions
1. **~500KB Temporal payload limit** — comments + subtasks + transcript grow the `documents` list
   (passed out of gather, into analyze). Mitigate via the caps above; never echo full source text in
   agent returns (only counts + bounded markdown/ADF).
2. **File-trigger payload key** — assume `payload["uploaded_files"]` (per docs); fall back to
   `transcript_file_key`.
3. **Multiple agents + one metadata.json** — verify the platform accepts two agents (review + post)
   discovered from `src/__init__.py`; both registered via `@agent(name=...)`.
4. **LlmFactory JSON reliability** — no native JSON mode; mitigated by strict prompt + tolerant
   `_extract_json`; `LLMPort` lets us swap to raw OpenAI JSON-mode if flaky.
5. **markdown→ADF subset** — supports the review format's headings/bullets/bold/paragraphs; exotic
   markdown degrades to plain paragraphs (documented, unit-tested).
6. **Subtask volume** — capped; epic→story children are NOT fetched (scope = subtasks + parent only,
   per decision).
7. **`@tool` registration** — `tools.py` imports the 4 `*_tool` modules only (internal packages have
   no decorators). Sources do async I/O ⇒ run only inside `gather_context`.

## Verification (end-to-end)
1. `cd demo_pranil && uv sync`.
2. Populate `.env` (Jira creds, `OPENAI_MODEL_ID`); optionally set `TENANT_ID` and upload a transcript
   file to that bucket. (OpenAI key comes from the platform runtime.)
3. `uv run pytest` — all pure + faked-network tests green (no live creds needed).
4. Two terminals: `aetherion run --tool` and `aetherion run --agent`.
5. **Draft (Agent A):**
   `aetherion agent JiraRequirementReview '{"issue_key":"ABC-123","include_parent":true,"include_subtasks":true,"uploaded_files":"meeting.txt"}'`
   → returns categorized findings + editable `comment_markdown` + ADF preview; **nothing posted** to Jira.
6. Human reviews/edits the returned markdown.
7. **Post (Agent B):**
   `aetherion agent PostJiraReviewComment '{"issue_key":"ABC-123","comment_markdown":"<approved text>"}'`
   → a structured comment appears on the issue (non-empty categories only; Evidence + Confidence; footer).
8. Negative checks: bad issue key ⇒ `status:"error"`; no transcript / no bucket ⇒ proceeds Jira-only;
   LLM unavailable ⇒ `completed_with_warnings`, no fabricated draft; post failure ⇒
   `completed_with_warnings` with error.
