# How the router works — the paths a run can take

**The guarantee:** every run either ends `ignored` — one of the closed
`IgnoreReason` set in `routing/ingress.py` — or attempts exactly one reply.
Tested for every path below by `tests/test_no_silent_failure.py`.

Every tool call in `agent/router_flow.py` goes through `_safe`, so an exception
becomes a result the run reports rather than a crashed workflow, which posts
nothing and emails nobody.

## Every way `run_router` can end (audited 2026-09-25, D0)

| # | Path | Person sees in Jira | Before D0 | Now |
|---|---|---|---|---|
| 1 | Ingress: no keyword / signature / own account / duplicate | nothing | deliberate | deliberate — `ignored` |
| 2 | Ingress: project outside `ORCH_ALLOWED_PROJECT_KEYS` | nothing | deliberate | deliberate — `ignored` |
| 3 | Ingress: no issue key / no comment on the issue | nothing | deliberate | deliberate — `ignored` (nowhere / nothing to reply to) |
| 4 | Ingress caught an exception (403, 500, network) | **nothing** | **swallowed** as `proceed: False` | "Could not read this ticket" + admin alert |
| 5 | The ingress tool itself raised (timeout, worker) | **nothing** | **swallowed** — workflow crashed | "Could not read this ticket" + admin alert |
| 6 | The classifier tool raised | **nothing** | **swallowed** — crash | treated as unreachable → "Which did you mean?" |
| 7 | Admin alert raised during a dispatch failure | **nothing** | **swallowed** — crash | reply posted, no Notification line |
| 8 | Outcome email raised | **nothing** | **swallowed** — crash | reply posted, "No email was sent — …" |
| 9 | The post itself raised | **nothing** | **swallowed** — crash, admins not told | admins alerted |
| 10 | Help, chatter, ambiguous, built, reviewed, child unreachable, child's answer unreadable | one reply | fine | unchanged |

Path 9 is the one the person can never see: the channel for telling them is
the thing that broke, so the administrators' alert is the only signal.

## Health check

    uv run aetherion agent JiraOrchestration '{"health_check": true}'

Reports, without touching any real ticket or sending any email:

- `agents` — each catalog agent, dispatched with the safe no-op payload
  (`issue_key: "FL"`): `{"reachable": true}` or the real error.
- `smtp` — whether the router's Gmail account can log in (`smtp_login_check`,
  login only): `{"ok": true}` or why not, e.g. "Gmail rejected the credentials
  (535). Use a 16-character app password …". Google revokes an app password
  when 2-Step Verification is reset, so run this after any account change.

## Which outcomes email (owner decision)

`ORCH_NOTIFY_ON=clarification,duplicates,failed`, the same in `LTW_NOTIFY_ON`.
**No email when tickets are created** — decided 2026-09-24, re-confirmed
2026-09-25. `tests/test_email_sending.py` fails if the two lists diverge or
"created" is added.
