# workos-shared

Shared Python modules for the Jarvis ecosystem — extracted in WorkOS E3 (Code Modularization Audit).

## Intent

Single source of truth for cross-repo helpers reused by `jarvis-rag`, `fireflies-agent`, `vault-keeper`, `scout`, `obsidian-web-mcp` etc. Eliminates drift between ad-hoc copies spread across 5+ codebases (37+ call sites for logger alone — see `20 - Projekty/System Jarvis/WorkOS/E3 — Code Audit Inventory.md`).

## Design rules

1. **Zero external dependencies in the core.** Core modules (logger, webhook, openrouter) use stdlib only. Modules that bind to a vendor SDK (like `anthropic_client`) expose the SDK via an *optional extra* (`pip install workos-shared[anthropic]`) and import it lazily.
2. **Additive, not enforcing.** Each module is opt-in. Consumers keep their local configuration (handlers, filters) via parameters.
3. **One module = one concern.** Logger is logger. HTTP client is HTTP client. No grab-bag helpers.
4. **Python 3.11+.** Matches the lowest version across adopter repos.

## Modules

| Module | Status | Purpose |
|---|---|---|
| `workos_shared.logger` | ✅ v0.1 | Structured JSON logging with rotating file + console + optional webhook. |
| `workos_shared.openrouter` | ✅ v0.2 (E2 S6) | Stdlib-only OpenRouter client for multi-provider LLM calls. |
| `workos_shared.anthropic_client` | ✅ v0.3 (E16 S2) | Anthropic API wrapper: Batch API + long-prompt routing + sync fallback. Requires `[anthropic]` extra. |
| `workos_shared.webhook` | ✅ v0.3 (E16 S2) | HMAC signature verification + persistent dedup (framework-agnostic). |
| `workos_shared.chromadb_store` | backlog | ChromaDB collection wrapper (Protocol-based). |
| `workos_shared.voyage_embedder` | backlog | Voyage AI embedding client. |
| `workos_shared.auth` | backlog | OAuth 2.1 helpers (PKCE + client credentials). |

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

## Quick start — anthropic_client (E16 S2)

```python
from workos_shared import call_claude

result = call_claude(
    api_key=ANTHROPIC_API_KEY,
    system="You are a helpful assistant.",
    user_message="Summarise this transcript: ...",
    custom_id="meeting-12345",
    long_prompt_safe=True,       # auto-switch to Sonnet 4.5 + 1M context for >540K chars
    batch_enabled=True,          # Batch API first (50% cheaper); falls back to sync on timeout
)
print(result.path, result.model, result.elapsed_s)  # e.g. "batch" "claude-sonnet-4-20250514" 124.3
```

Install the optional `anthropic` SDK: `pip install workos-shared[anthropic]` (or keep it in the consumer's own `pyproject.toml` — the wrapper imports it lazily).

## Quick start — webhook (E16 S2)

```python
from workos_shared import verify_hmac_signature, PersistentDedup, SignatureMismatch

try:
    verify_hmac_signature(body=raw_bytes, signature=request.headers["x-hub-signature"], secret=SECRET)
except SignatureMismatch:
    return ("forbidden", 403)

dedup = PersistentDedup("/var/lib/myservice/processed.ids")
if not dedup.add(meeting_id):
    return ("already processed", 200)
# ... process ...
```

## Migrating an existing service

See `docs/MIGRATION.md`.

## License

Proprietary — Grzegorz Golaś / Jarvis ecosystem.
