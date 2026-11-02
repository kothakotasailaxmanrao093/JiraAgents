"""The one place the agents' memory is stored (2026-10-30), through Aetherion's
own Memsy service, ``common_lib.memsy`` (2026-11-02).

Orchestration is the only agent that talks to the memory service. Requirement
Review and Planning are given what they need as input and hand back what is
to be kept. Every call to the service is in this file: ``write``, ``search``,
``status`` and ``listed_metadata``.

The platform's ``MemsyService`` writes, searches and reports status. Its
``memories.list`` returns the right total but no items (2026-11-02), so
listing uses the same Aetherion client it wraps (``_get_raw_client``), which
lists correctly. The service never raises: an empty write is a failed write.

Memsy rewrites the text it stores and cannot update or delete, so exact data
is kept as numbered SNAPSHOTS in each event's metadata, which Memsy returns
unchanged (``payload.source_metadata``):

- every save writes snapshot N+1, the whole state; reading takes the newest
  complete snapshot, so a newer one replaces an older one without deleting it;
- the data is compressed, then encrypted (AES-256-GCM, ``CODE_GRAPH_KEY``)
  before it leaves the agent, in the format Planning wrote its TDD memory in
  (``zlib-aesgcm-1``), so that memory is read as it is;
- a snapshot too big for Memsy's 4 KB metadata is split into parts, each in
  its own session, because Memsy merges similar events of one session;
- a save waits until Memsy has processed it, so the next request reads it.

The service reads ``MEMSY_API_KEY`` and ``MEMSY_BASE_URL`` from the
environment; they and the key are taken from the platform's secret files first
(``shared.settings.secret``).
"""

from __future__ import annotations

import ast
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
import zlib
from functools import lru_cache
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from shared.settings import secret

logger = logging.getLogger(__name__)

KEY_NAME = "CODE_GRAPH_KEY"
FORMAT = "zlib-aesgcm-1"
PART_CHARS = 3_500  # Memsy keeps at most 4,096 characters of metadata per event
LIST_PAGE = 100
LIST_PAGES = 10  # the newest 1,000 memories of an actor are read
WAIT_SECONDS = 30
_PREFIX = "enc:v1:"
_INFO = b"aetherion/code-graph/v1/"
_LABEL = "MemsySnapshot"


class MemoryUnavailable(RuntimeError):
    """Memsy or the key is not configured, or Memsy is not reachable."""


def project_of(key: str) -> str:
    return key.split("-")[0].strip().upper() if key else ""


def service() -> Any:
    """Aetherion's Memsy service; raises when it is not configured."""
    for name in ("MEMSY_API_KEY", "MEMSY_BASE_URL"):
        if not os.environ.get(name, "").strip() and secret(name):
            # The service reads the environment; a mounted secret file is passed on.
            os.environ[name] = secret(name)
    from common_lib.memsy import get_memsy_service

    memsy = get_memsy_service()
    if not memsy.is_configured:
        raise MemoryUnavailable("MEMSY_API_KEY and MEMSY_BASE_URL are not set")
    return memsy


def _lister() -> Any:
    """The Aetherion client the service wraps, for listing (see the module's note)."""
    service()
    from common_lib.memsy.client import _get_raw_client

    return _get_raw_client()


def payload_of(item: Any) -> dict:
    """A listed memory's payload, which Memsy may return as a dict or its text."""
    payload = getattr(item, "payload", None)
    if isinstance(payload, dict):
        return payload
    for parse in (json.loads, ast.literal_eval):
        try:
            value = parse(str(payload))
            if isinstance(value, dict):
                return value
        except (ValueError, SyntaxError):
            continue
    return {}


def metadata_of(sources: Any) -> list[dict]:
    """The event metadata in a result's ``source_metadata``, which the service
    returns unchanged."""
    out = []
    for source in sources or []:
        meta = source.get("metadata") if isinstance(source, dict) else None
        if isinstance(meta, dict):
            out.append(meta)
    return out


async def listed_metadata(actor: str) -> list[dict]:
    """Every event metadata of ``actor``'s newest memories."""
    lister, out = _lister(), []
    for page in range(LIST_PAGES):
        listed = await lister.memories.list(
            actor_id=actor, limit=LIST_PAGE, offset=page * LIST_PAGE
        )
        for item in listed.items or []:
            out += metadata_of(payload_of(item).get("source_metadata"))
        if len(listed.items or []) < LIST_PAGE:
            break
    return out


async def write(events: list[Any]) -> list[str]:
    """Writes events; their ids. Raises when the service did not take them."""
    ids = await service().ingest(events)
    if not ids:
        raise MemoryUnavailable("Memsy did not accept the events (see its warning above)")
    return list(ids)


async def search(query: str, actor: str, limit: int, threshold: float = 0.0) -> list[dict]:
    """Memories of ``actor`` about ``query`` by meaning: [{"score", "content",
    "metadata": [event metadata]}], best first; [] when none or on failure."""
    found = await service().search_rich(
        query=query, actor_id=actor, limit=limit, threshold=threshold
    )
    return [
        {
            "score": float(r.get("score") or 0.0),
            "content": str(r.get("content") or ""),
            "metadata": metadata_of(r.get("source_metadata")),
        }
        for r in found
    ]


def event(actor: str, session: str, kind: str, content: str, metadata: dict, team: str) -> Any:
    """One event as the service's ``ingest`` takes it (Memsy's EventPayload)."""
    from memsy import EventPayload

    return EventPayload(
        actor_id=actor,
        session_id=session,
        kind=kind,
        content=content,
        team_id=team,
        metadata=json.dumps(metadata, sort_keys=True),
    )


# --- encryption (Planning's code_crypto format) ---------------------------------------------


@lru_cache(maxsize=4)
def _master(raw: str) -> bytes:
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise MemoryUnavailable(f"{KEY_NAME} is not valid base64") from None
    if len(key) != 32:
        raise MemoryUnavailable(f"{KEY_NAME} must be 32 random bytes, base64-encoded")
    return key


def _master_key() -> bytes:
    raw = secret(KEY_NAME)
    if not raw:
        raise MemoryUnavailable(f"{KEY_NAME} is not set — the memory is never kept unencrypted")
    return _master(raw)


@lru_cache(maxsize=256)
def _derive(master: bytes, actor: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA512(), length=32, salt=None, info=_INFO + b"enc/" + actor.encode()
    ).derive(master)


@lru_cache(maxsize=4)
def _key_id(master: bytes) -> str:
    return hmac.new(master, _INFO + b"key-id", hashlib.sha512).hexdigest()[:8]


def _aad(actor: str, seq: int) -> bytes:
    return "\x1f".join(("v1", _LABEL, actor, str(seq), "data")).encode("utf-8")


def seal(data: dict, actor: str, seq: int) -> str:
    packed = base64.b64encode(zlib.compress(json.dumps(data, sort_keys=True).encode(), 9))
    master = _master_key()
    nonce = os.urandom(12)
    sealed = AESGCM(_derive(master, actor)).encrypt(nonce, packed, _aad(actor, seq))
    return f"{_PREFIX}{_key_id(master)}:{base64.b64encode(nonce + sealed).decode('ascii')}"


def unseal(text: str, actor: str, seq: int) -> dict:
    if not text.startswith(_PREFIX):
        raise MemoryUnavailable(f"snapshot {seq} of {actor} is not encrypted")
    master = _master_key()
    key_id, _, body = text[len(_PREFIX) :].partition(":")
    if key_id != _key_id(master):
        raise MemoryUnavailable(f"snapshot {seq} of {actor} is under another {KEY_NAME}")
    try:
        blob = base64.b64decode(body, validate=True)
        packed = AESGCM(_derive(master, actor)).decrypt(blob[:12], blob[12:], _aad(actor, seq))
    except (InvalidTag, binascii.Error, ValueError):
        raise MemoryUnavailable(f"snapshot {seq} of {actor} could not be decrypted") from None
    return json.loads(zlib.decompress(base64.b64decode(packed)))


# --- snapshots ------------------------------------------------------------------------------


async def load(actor: str, kind: str) -> tuple[int, dict]:
    """(seq, snapshot) of the newest complete snapshot; (0, {}) when none."""
    parts: dict[int, dict[int, str]] = {}
    sizes: dict[int, int] = {}
    for meta in await listed_metadata(actor):
        if (meta.get("type"), meta.get("actor"), meta.get("format")) != (kind, actor, FORMAT):
            continue
        seq = int(meta["seq"])
        parts.setdefault(seq, {})[int(meta["part"])] = str(meta["data"])
        sizes[seq] = int(meta["parts"])
    for seq in sorted(parts, reverse=True):
        if len(parts[seq]) == sizes[seq]:
            return seq, unseal("".join(parts[seq][i] for i in range(sizes[seq])), actor, seq)
    return 0, {}


async def save(actor: str, session: str, kind: str, seq: int, snapshot: dict, summary: str) -> None:
    """Writes snapshot ``seq`` and waits until it can be read back."""
    sealed = seal(snapshot, actor, seq)
    chunks = [sealed[i : i + PART_CHARS] for i in range(0, len(sealed), PART_CHARS)]
    events = [
        event(
            actor,
            f"{session}:{seq}:{i + 1}",
            "app_event",
            f"{summary} Memory snapshot {seq}, part {i + 1} of {len(chunks)}.",
            {
                "type": kind,
                "actor": actor,
                "format": FORMAT,
                "seq": seq,
                "part": i,
                "parts": len(chunks),
                "data": chunk,
            },
            project_of(actor.split(":", 1)[-1]),
        )
        for i, chunk in enumerate(chunks)
    ]
    ids = await write(events)
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        status = await service().status(ids)
        if status.get("failed_ids"):
            raise MemoryUnavailable(f"Memsy could not process the memory snapshot: {status}")
        if not status.get("pending_ids") and (await load(actor, kind))[0] >= seq:
            return
        await asyncio.sleep(1.5)
    logger.warning("Memsy snapshot %s %s not readable after %ss", actor, seq, WAIT_SECONDS)
