# `shared/` — the foundation both Jira agents stand on

One definition of the things every Jira agent needs: reading attachments,
reading and writing Atlassian Document Format, recognising Confluence links,
reading configuration, and the result contract a child agent hands back to the
router.

**This directory is the only place these are edited.** Each agent project
carries a mirrored copy at `<project>/src/shared/`, written by
`scripts/sync_shared.py`. Editing a mirror directly fails the build.

---

## Why a mirror instead of a package

The Aetherion packer builds one tarball per project from **that project's
`src/` directory**. A package living only in a sibling folder is simply not
deployed, so the shared code has to be physically present inside each project.

A real wheel published to the platform's CodeArtifact index would avoid the
copy, and the packer supports it (it preserves non-platform dependencies and
merges `[tool.uv.sources]`). It needs publish credentials this project does not
have today, so the mirror is the honest choice: one canonical definition, copies
that cannot silently drift.

---

## The three packaging rules that shape this directory

All three were verified against the installed SDK, not assumed.

**1. Every `__init__.py` is dropped from the deploy tarball.**

`aetherion_sdk.packer.GLOBAL_IGNORE` lists `__init__.py`. Anything defined in
one exists locally and is missing on the worker — tests pass, production fails
with `AttributeError` on first use.

**2. …but a local `__init__.py` is required for the package to ship at all.**

`cli._detect_packages()` only finds directories under `src/` that contain one.
Without it, `shared/` is absent from the tarball entirely.

Both rules at once mean: the mirror's `__init__.py` **must exist and must stay
empty**. `sync_shared.py` writes it; `test_shared_in_sync.py` fails if anyone
puts code in it.

**3. Modules here use relative imports, and that is deliberate.**

The two agents import under different roots:

```python
from src.shared import adf   # jira-task-creation
from shared import adf       # jira-requirement-review
```

A module that says `from .settings import env_bool` resolves correctly under
both, because relative imports follow `__package__`. Absolute imports inside
this package would force every agent onto one root. **Never write
`from shared.x import y` or `from src.shared.x import y` inside this directory.**

---

## Modules

| Module | What it holds | Pure? |
|---|---|---|
| `settings.py` | `env_bool` / `env_int` / `env_float` / `env_list`, `mask` | yes |
| `adf.py` | ADF node builders, `blocks_to_doc`, `to_text`, `markdown_to_adf` | yes |
| `attachments.py` | PDF/Word/Excel/PPT/CSV/JSON/text/image extraction, caps, middle-out truncation | yes |
| `confluence_text.py` | Confluence URL parsing, same-site guard, storage-format → text | yes |
| `keywords.py` | trigger matching, the agent signature and footer | yes |
| `contract.py` | `AgentResult` — what a child returns to the router | yes |
| `transport.py` | the narrow HTTP `Transport` protocol | yes |
| `confluence_fetch.py` | page and attachment reading, through the port | yes |
| `_logging.py` | platform logger with a stdlib fallback | yes |

Every module is free of network I/O. Code that needs HTTP takes a
`Transport` (see below) rather than importing `httpx` or `aiohttp`.

### Why `to_text` is the one from the work-breakdown agent

The review agent used to flatten every ADF text node onto one line joined by spaces. A
ticket's acceptance criteria arrived looking like this:

```
Acceptance Criteria The driver must be able to: record a reading see past
readings Latency must be under 2s.
```

An agent asked to report *missing acceptance criteria* cannot see that a list
exists in that. `to_text` keeps the structure.

### Why there is a `Transport` protocol

The work-breakdown agent speaks `httpx`; the review agent speaks `aiohttp`. Both are working,
tested production code with no behavioural reason to change. Shared code
therefore depends on a three-method protocol — `get_json`, `get_bytes`,
`post_json` — and each agent supplies a small adapter over the client it already
builds. Dependency Inversion: the shared policy owns the interface, the agents
conform, and neither HTTP layer is rewritten.

---

## Working on this package

```bash
# 1. edit the canonical module
vim shared/adf.py

# 2. format and lint to the agents' settings
uv run --project jira-task-creation black --line-length 100 shared/
uv run --project jira-task-creation ruff  check shared/ --line-length 100

# 3. mirror into every project
python scripts/sync_shared.py

# 4. both suites must pass — they consume it
cd jira-task-creation && uv run pytest -q
cd ../jira-requirement-review        && uv run pytest -q
```

`python scripts/sync_shared.py --check` verifies without writing, and is what a
CI job should run.

Adding a fourth agent is one entry in `TARGETS` in `scripts/sync_shared.py`,
plus a copy of `tests/test_shared_in_sync.py` in the new project.
