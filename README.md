# workos-shared

Shared Python modules for the Jarvis ecosystem — extracted in WorkOS E3 (Code Modularization Audit).

## Intent

Single source of truth for cross-repo helpers reused by `jarvis-rag`, `fireflies-agent`, `vault-keeper`, `scout`, `obsidian-web-mcp` etc. Eliminates drift between ad-hoc copies spread across 5+ codebases (37+ call sites for logger alone — see `20 - Projekty/System Jarvis/WorkOS/E3 — Code Audit Inventory.md`).

## Design rules

1. **Zero external dependencies.** `workos_shared` uses stdlib only. Any heavy dep (httpx, chromadb, voyage) goes in the *consumer* repo, not here.
2. **Additive, not enforcing.** Each module is opt-in. Consumers keep their local configuration (handlers, filters) via parameters.
3. **One module = one concern.** Logger is logger. HTTP client is HTTP client. No grab-bag helpers.
4. **Python 3.11+.** Matches the lowest version across adopter repos.

## Modules

| Module | Status | Purpose |
|---|---|---|
| `workos_shared.logger` | ✅ v0.1 | Structured JSON logging with rotating file + console + optional webhook. |
| `workos_shared.http_client` | backlog (E3 S4) | httpx wrapper with retry + rate-limit. |
| `workos_shared.chromadb_store` | backlog (E3 S4) | ChromaDB collection wrapper (Protocol-based). |
| `workos_shared.voyage_embedder` | backlog (E3 S4) | Voyage AI embedding client. |
| `workos_shared.auth` | backlog (E3 S4) | OAuth 2.1 helpers (PKCE + client credentials). |

## Install

Editable install in consumer repo:

```bash
pip install -e /path/to/workos-shared
# or with uv:
uv pip install -e /path/to/workos-shared
```

For VPS deployments, clone alongside the consumer repo:

```bash
cd /home/ccuser
git clone git@github.com:greegorij/workos-shared.git
uv pip install -e /home/ccuser/workos-shared  # or pip, matches consumer's env
```

## Quick start — logger

```python
from workos_shared.logger import get_logger

logger = get_logger(__name__, service_name="jarvis-rag")
logger.info("indexing started", extra={"context": {"chunks": 42, "vault": "Gregor"}})
```

Default behaviour: JSON one-line per entry, STDOUT + rotating file at `/tmp/workos-logs/jarvis-rag.log` (override via `LOG_DIR`).

Dev mode (plain text):

```python
logger = get_logger(__name__, service_name="dev", structured=False)
```

Optional webhook for `ERROR+`:

```bash
export WEBHOOK_URL="https://hooks.slack.com/..."
```

## Migrating an existing service

See `docs/MIGRATION.md`.

## License

Proprietary — Grzegorz Golaś / Jarvis ecosystem.
