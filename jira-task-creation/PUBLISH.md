# Publishing

How to deploy JiraTaskCreation to the Aetherion platform, and how to confirm it
actually went live.

Follow every step. Each one exists because skipping it has caused a real
production problem, and the section [Why each step exists](#why-each-step-exists)
names which.

---

## Before you begin

Everything runs from the project directory:

```bash
cd /path/to/jira-task-creation
```

If `aetherion publish` reports that it cannot find the project, you are in the
wrong directory. This is easy to do — the parent folder looks similar.

You also need to be logged in. If publishing fails with an authentication
error, your token has expired:

```bash
uv run aetherion login
```

---

## The checklist

Copy this, tick as you go.

```
[ ] 1. Tests, lint and formatting all pass
[ ] 2. Version bumped in pyproject.toml
[ ] 3. .env reviewed — it ships inside the package
[ ] 4. Package built and inspected locally
[ ] 5. Published
[ ] 6. Confirmed live from the logs
```

---

## Step 1 — Check everything passes

```bash
uv run pytest -p no:warnings
uv run ruff check src tests
uv run black --check src tests
```

Expect `853 passed`, `All checks passed!`, and `would be left unchanged`.

**Do not publish with a failing test.** Most of these tests exist because
something broke in production; a failure is usually telling you about a real
case, not an outdated assertion.

---

## Step 2 — Bump the version

**This is the step people skip, and it is the one that matters most.**

```bash
grep -n '^version' pyproject.toml
```

Edit `pyproject.toml` and raise the number:

```toml
version = "4.1.1"     # was 4.1.0
```

Or from the command line, substituting the real current version:

```bash
sed -i '' 's/^version = "4.1.0"$/version = "4.1.1"/' pyproject.toml
grep -n '^version' pyproject.toml
```

| Change | Bump |
|---|---|
| Bug fix, wording, config | patch — `4.1.0` → `4.1.1` |
| New capability | minor — `4.1.0` → `4.2.0` |
| Breaking change to settings or behaviour | major — `4.1.0` → `5.0.0` |

> **Publishing the same version over itself may leave the old build running.**
> The upload returns success and nothing changes. This happened with 4.0.9: the
> platform reported a successful publish while the worker kept serving the old
> code, and every run kept crashing with an error that had already been fixed.

### There are two version numbers — keep them in step

`src/agent/metadata.json` carries its **own** `version`, and that is the one the
platform registers the agent under. If it disagrees with `pyproject.toml`, the
Aetherion UI shows a different number from the one you bumped.

```bash
grep -n '^version' pyproject.toml
grep -n '"version"' src/agent/metadata.json
```

Both must read the same. They drifted once — `pyproject.toml` was bumped three
times while `metadata.json` stayed at `4.0.9`, so the UI reported `4.0.9` for
every one of those deployments.

---

## Step 3 — Review `.env`

**`.env` ships inside the deployed package.** Whatever is in it at the moment
you publish is what the deployed agent uses.

```bash
grep -n "^LTW_NOTIFY_ON\|^LTW_READ_ONLY\|^LTW_ALLOWED_PROJECT_KEYS" .env
```

Check at minimum:

| Setting | Confirm |
|---|---|
| `LTW_NOTIFY_ON` | Is this who you want emailed, and for what? |
| `LTW_READ_ONLY` | Must be `false` in production, or nothing is ever created |
| `LTW_ALLOWED_PROJECT_KEYS` | Restricted to the projects you intend |
| `LTW_ATTACHMENT_READ_IMAGES` | `true` costs a model call per image and slows runs |

> Version 4.0.11 shipped with `failed` in `LTW_NOTIFY_ON` because the file was
> edited *after* the package was built. Every stray event then sent an email.
> Review this file **before** step 4, not after.

`LTW_LOCAL_EXECUTION` is read only by `src/webhook/server.py`, which the
platform does not run, so its value is harmless in a deployed package.

---

## Step 4 — Build and inspect locally

`aetherion publish` packs the project, uploads it, then **deletes the
artifact** — so this is the only chance to see what is actually being sent.

```bash
uv run python scripts/build_package.py --extract
```

Then check three things:

**The version that will ship**

```bash
grep -n '^version' dist/package/pyproject.toml
```

**The settings that will ship** — note `.env` does not appear in the printed
manifest because it is a dotfile, but it *is* in the package:

```bash
grep -n "^LTW_NOTIFY_ON\|^LTW_READ_ONLY" dist/package/.env
```

**Your actual change** — grep the packaged file for it, not your working copy:

```bash
grep -n "<something from your change>" dist/package/agent/agent.py
```

---

## Step 5 — Publish

```bash
uv run aetherion publish
```

A successful run ends with:

```
Scanning for agent + tool files in 'src'...
   Found 24 files.
Created artifact: dist/package.tar.gz
Uploading package.tar.gz to https://test.sbox.aetherion.io/api/v1/agent/register...
HTTP/1.1 202 Accepted
Upload accepted, registration in progress (workflow_id=agent-bundle-...)
Cleaned up dist/package.tar.gz
```

**Write down the `workflow_id`.** It identifies this deployment if you need to
ask about it.

### What `202 Accepted` does and does not mean

| It means | It does **not** mean |
|---|---|
| The upload was received | The new build is running |
| Registration is queued | The worker has restarted |

The command returns **before** the worker swaps. Step 6 is not optional.

---

## Step 6 — Confirm it is actually live

Publishing successfully and running the new code are different things. Confirm
with a real run.

**1.** Pick a ticket that is not currently busy. Avoid any ticket with stuck or
retrying workflows — those replay old code and will confuse the result.

**2.** Comment a simple requirement:

```
@Aetherion Let drivers photograph vehicle damage at handover.
```

**3.** Check the logs for a line that exists only in your new build. Line
numbers shift whenever you add code above them, which makes them a reliable
fingerprint:

```bash
grep -n "mode :" src/agent/agent.py
```

Whatever line number that prints locally is what the logs must show. If the
logs show the **previous** build's line number, the worker has not swapped.

At the time of writing that is line **361**, so a live 4.1.0 logs:

```
src.agent.agent:361 - mode : webhook | issue_key : FL-123
```

Two other useful markers:

| Log line | Means |
|---|---|
| `Created in FL: 4 issue(s) [epic=... stories=[...] ...]` | Creation succeeded, and names exactly what was made |
| `Reported outcome on FL-123 (comment 10501, labels ...)` | The reply comment was posted |
| `RestrictedWorkflowAccessError` | **The deployed build is older than your code.** Go back to step 2 |

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `aetherion publish` cannot find the project | Wrong directory | `cd` into `JiraTaskCreation/` |
| Authentication failed | Expired token | `uv run aetherion login` |
| Publish succeeds, behaviour unchanged | Version not bumped | Bump it and publish again |
| `RestrictedWorkflowAccessError` in logs | Old build still running | Bump the version and republish |
| Wrong settings in production | `.env` shipped with wrong values | Fix `.env`, bump, republish |
| Flood of identical emails | A run is failing and retrying | Fix the run. `LTW_EMAIL_REPEAT_WINDOW` caps the noise but is not the fix |

### Rolling back

There is no rollback command. To revert, restore the previous code, bump the
version **forward** (never reuse an old number), and publish again.

---

## Why each step exists

Each of these is a real incident from this project, not a hypothetical.

| Step | What happened without it |
|---|---|
| **2 — Bump the version** | 4.0.9 was published over 4.0.9. The upload succeeded, the worker kept the old code, and every run crashed with `RestrictedWorkflowAccessError` — an error already fixed in the source |
| **3 — Review `.env`** | 4.0.11 shipped `LTW_NOTIFY_ON=...,failed` because the file was edited after the build. Stray webhook events then emailed on every retry |
| **4 — Inspect the package** | `.env` is invisible in the printed manifest because it is a dotfile. A wrong value travelled to production unnoticed |
| **6 — Confirm from the logs** | A successful upload was assumed to be a successful deployment. It was not, and hours were spent debugging code that was never running |

---

## Quick reference

```bash
cd /path/to/jira-task-creation

# 1. verify
uv run pytest -p no:warnings
uv run ruff check src tests
uv run black --check src tests

# 2. bump  (edit BOTH — REQUIRED)
grep -n '^version' pyproject.toml
grep -n '"version"' src/agent/metadata.json

# 3. review what ships
grep -n "^LTW_NOTIFY_ON\|^LTW_READ_ONLY" .env

# 4. build and inspect
uv run python scripts/build_package.py --extract
grep -n '^version' dist/package/pyproject.toml

# 5. publish
uv run aetherion publish

# 6. confirm from the logs after one real comment
grep -n "mode :" src/agent/agent.py
```
