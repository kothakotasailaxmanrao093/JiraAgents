"""Print the CanonicalEvent that antennae would produce, without AWS.

Point Jira's Automation rule at this through ngrok and every delivery prints
the headers, the raw body, and the event the unified-webhook Lambda would have
published to SQS — using the **real** JiraPlugin, not a copy of it.

That answers the two questions the Lambda cannot, because by the time it fails
you only see a 401 or an empty queue:

* Is the ``X-Jira-Webhook-Secret`` header arriving at all?
* Does the body produce ``event_type: issue.commented``, or does it fall
  through to ``unknown.received`` because ``event_type`` was never sent?

Nothing is written to Jira, SQS or anywhere else. It reads and prints.

    python scripts/canonical_probe.py              # then: ngrok http 8001
    POST http://localhost:8001/webhooks/v2/jira?tenant_id=test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Imported at module level, not inside main(): ``from __future__ import
# annotations`` makes every annotation a string, and FastAPI resolves those
# against module globals. A function-local import leaves ``Request`` unresolvable,
# and FastAPI then treats it as a required query parameter.
import uvicorn
from fastapi import FastAPI, Request

# The plugin lives in the antennae repo and imports its neighbours by bare name
# ("from canonical_event import ..."), so its own directory has to be on the
# path rather than the package root.
DEFAULT_ANTENNAE = (
    Path(__file__).resolve().parents[2] / "antennae-main" / "webhooks" / "aws" / "unified-webhook"
)


def load_plugin(unified_webhook_dir: Path):
    if not (unified_webhook_dir / "providers" / "jira.py").exists():
        raise SystemExit(
            f"No unified-webhook at {unified_webhook_dir}\n"
            f"Pass the right path with --antennae /path/to/webhooks/aws/unified-webhook"
        )
    sys.path.insert(0, str(unified_webhook_dir))
    from providers.jira import JiraPlugin  # noqa: PLC0415

    return JiraPlugin()


def describe(plugin, headers: dict[str, str], body: dict[str, Any], query: dict[str, str]) -> None:
    """Print what the Lambda would have done with this delivery."""
    line = "=" * 78
    print(f"\n{line}\nDELIVERY RECEIVED\n{line}")

    # --- 1. the secret header -----------------------------------------
    supplied = headers.get("x-jira-webhook-secret")
    print("\n1. AUTHENTICATION")
    if supplied:
        print(f"   x-jira-webhook-secret : present ({len(supplied)} chars)")
        print("   -> the Lambda would compare this against JIRA_WEBHOOK_SECRET")
    else:
        print("   x-jira-webhook-secret : MISSING  <-- this is a 401")
        print("   -> native Jira System Webhooks cannot send headers at all.")
        print("      Use Automation for Jira -> Send web request -> Headers.")

    # --- 2. the tenant -------------------------------------------------
    print("\n2. TENANT")
    tenant_id = query.get("tenant_id")
    if tenant_id:
        print(f"   ?tenant_id= : {tenant_id}")
    else:
        print("   ?tenant_id= : MISSING  <-- TenantResolutionError, nothing is published")
    if query.get("tenant"):
        print(f"   ?tenant=    : {query['tenant']} (only needed for a per-tenant secret)")

    # --- 3. the canonical event ---------------------------------------
    print("\n3. CANONICAL EVENT")
    event = plugin.to_canonical(headers, body, tenant_id or "<none>", {})
    as_dict = event.model_dump() if hasattr(event, "model_dump") else event.__dict__
    print(json.dumps(as_dict, indent=2, default=str)[:4000])

    # --- 4. what the subscription needs -------------------------------
    payload = as_dict.get("structured_payload") or {}
    comment = payload.get("comment") or {}
    print("\n4. WHAT A SUBSCRIPTION CAN MATCH ON")
    event_type = as_dict.get("event_type")
    ok = event_type == "issue.commented"
    print(
        f"   event_type            : {event_type}  {'' if ok else '<-- expected issue.commented'}"
    )
    if not ok:
        print('      -> send a top-level "event_type" in the Automation rule\'s custom body,')
        print("         or the subscription filter will never match.")
    print(f"   structured_payload.issue_key : {payload.get('issue_key') or 'MISSING'}")
    print(
        f"   structured_payload.comment.id: {comment.get('id') or 'MISSING  <-- agent needs this'}"
    )
    body_text = str(comment.get("body") or "")
    print(f"   comment.body contains @Aetherion   : {'@Aetherion' in body_text}")
    print(
        f"   comment.body contains AetherionAgent: {'AetherionAgent' in body_text}"
        f"{'   <-- the loop guard must exclude this' if 'AetherionAgent' in body_text else ''}"
    )
    print(f"{line}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001, help="default 8001, to leave 8000 free")
    parser.add_argument("--antennae", type=Path, default=DEFAULT_ANTENNAE)
    args = parser.parse_args()

    plugin = load_plugin(args.antennae)

    app = FastAPI(title="canonical-probe")

    @app.post("/webhooks/v2/{provider_slug}")
    async def probe(provider_slug: str, request: Request) -> dict[str, Any]:
        raw = await request.body()
        # The Lambda lowercases every header key before the plugin sees them.
        headers = {k.lower(): v for k, v in request.headers.items()}
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            print(f"\n!! Body is not valid JSON ({len(raw)} bytes) — the Lambda returns 400 here.")
            print(f"   An unescaped quote or newline in a smart value does this.\n{raw[:400]!r}\n")
            return {"ok": False, "reason": "invalid_json"}

        describe(plugin, headers, body, dict(request.query_params))
        return {"ok": True, "note": "probe only — nothing was published or created"}

    @app.get("/")
    async def index() -> dict[str, str]:
        return {"post_to": f"http://localhost:{args.port}/webhooks/v2/jira?tenant_id=test"}

    print(f"Probe listening on :{args.port}")
    print(f"Plugin: {args.antennae}")
    print("Point Jira at  <ngrok-url>/webhooks/v2/jira?tenant_id=test\n")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
