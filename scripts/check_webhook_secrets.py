"""Does the platform's webhook service accept OUR secrets? (2026-10-16)

Run it yourself; it asks for each secret with hidden typing and never prints,
stores or sends it anywhere except as the signature/header the real webhook uses.

    python3 scripts/check_webhook_secrets.py

For each of Jira and Confluence it sends one test event to the platform's URL:

    401 Invalid signature  -> the platform does NOT have this secret for our tenant
    200 / 202              -> the secret matches; the platform accepted the event

The test events name no real comment (comment id 0), so if one is accepted the
agent finds nothing to answer and does nothing. Press Enter to skip a secret.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import urllib.error
import urllib.request

BASE = "https://wh.sbox.aetherion.io/webhooks/v2"
TENANT = "tenant_id=cac3f93c-084e-ad2c-77fc-18db56e1ebb5"


def send(url: str, body: bytes, headers: dict[str, str]) -> str:
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return f"{resp.status} {resp.read()[:200].decode(errors='replace')}"
    except urllib.error.HTTPError as exc:
        return f"{exc.code} {exc.read()[:200].decode(errors='replace')}"


def verdict(answer: str) -> str:
    if answer.startswith(("200", "202")):
        return "✅ MATCHES: the platform accepted this secret"
    if answer.startswith("401"):
        return "❌ NO MATCH: the platform does not have this secret for our tenant"
    return "⚠️  unexpected answer, send it to the platform team"


def main() -> None:
    tenant = input("Tenant name from the webhook URL (the value after '&tenant=', e.g. test): ").strip()
    query = f"?{TENANT}&tenant={tenant}" if tenant else f"?{TENANT}"

    secret = getpass.getpass("Jira webhook secret (hidden, Enter to skip): ").strip()
    if secret:
        body = json.dumps(
            {
                "webhookEvent": "comment_created",
                "issue": {"key": "BTS-15", "fields": {"project": {"key": "BTS"}}},
                "comment": {"id": "0", "body": "webhook secret check"},
            }
        ).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        answer = send(f"{BASE}/jira{query}", body, {"X-Hub-Signature": signature})
        print(f"\nJira:       {answer}\n            {verdict(answer)}")

    secret = getpass.getpass("\nConfluence webhook secret (hidden, Enter to skip): ").strip()
    if secret:
        body = json.dumps(
            {"event": "comment_created", "page_id": "0", "comment_id": "0", "space_key": "BTS"}
        ).encode()
        answer = send(f"{BASE}/confluence{query}", body, {"X-Confluence-Webhook-Secret": secret})
        print(f"\nConfluence: {answer}\n            {verdict(answer)}")


if __name__ == "__main__":
    main()
