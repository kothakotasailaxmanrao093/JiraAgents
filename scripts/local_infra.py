"""Neo4j and object storage for the agents on this laptop (2026-10-08).

Planning keeps its code graph in Neo4j and writes the technical design (a PDF)
to object storage; Coding looks a service's repository up in the same graph
and reads the design back from the same storage; the router puts the design on
the Jira ticket. A request from the router's form (2026-10-08) also passes Task
Creation's breakdown PDF to Review through that storage — so, as on the
platform, every agent shares one bucket. On Aetherion the platform provides storage and the agents'
own .env name their Neo4j. On a laptop both run in Docker:

    planning-neo4j   Neo4j 5.26           bolt://localhost:7687
    planning-s3      RustFS (S3-like)     http://localhost:9000, bucket planning-local

Their passwords are read from the containers when a worker starts and live in
that worker's memory only — never written to a file. A value already in the
shell or the agent's own .env wins: these only fill what is missing.

Called by ``scripts/local_worker.py`` — never by an agent.
"""

from __future__ import annotations

import os
import subprocess

# The agents that use the code graph; every agent shares the storage.
GRAPH_USERS = ("planning-agent", "coding-agent")

NEO4J_CONTAINER = "planning-neo4j"
STORAGE_CONTAINER = "planning-s3"
DEFAULT_BUCKET = "planning-local"


def container_env(name: str) -> dict[str, str]:
    """A running container's environment, or {} when Docker or it is not there."""
    try:
        out = subprocess.run(
            [
                "docker",
                "inspect",
                name,
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def settings(neo4j: dict[str, str], storage: dict[str, str]) -> dict[str, str]:
    """What the agents read, from the two containers' own settings."""
    values: dict[str, str] = {}
    user, _, password = (neo4j.get("NEO4J_AUTH") or "").partition("/")
    if password:
        values |= {
            "NEO4J_URI": "bolt://localhost:7687",
            "NEO4J_USER": user or "neo4j",
            "NEO4J_PASSWORD": password,
        }
    if storage.get("RUSTFS_ACCESS_KEY") and storage.get("RUSTFS_SECRET_KEY"):
        values |= {
            # common_lib's storage client takes these keys only with APP_ENV=local.
            "APP_ENV": "local",
            "STORAGE_ENDPOINT": "http://localhost:9000",
            "STORAGE_SECURE": "false",
            "STORAGE_ACCESS_KEY": storage["RUSTFS_ACCESS_KEY"],
            "STORAGE_SECRET_KEY": storage["RUSTFS_SECRET_KEY"],
            "TENANT_ID": os.environ.get("LOCAL_STORAGE_BUCKET") or DEFAULT_BUCKET,
        }
    return values


def install(agent: str) -> list[str]:
    """Fill this worker's missing graph and storage settings; the names filled."""
    neo4j = container_env(NEO4J_CONTAINER) if agent in GRAPH_USERS else {}
    values = settings(neo4j, container_env(STORAGE_CONTAINER))
    filled = [name for name in values if not os.environ.get(name)]
    for name in filled:
        os.environ[name] = values[name]
    return filled
