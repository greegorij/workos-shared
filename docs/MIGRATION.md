# Migration guide — adopting `workos_shared.logger`

Follow this runbook when adding the shared logger to a new service (vault-mcp, scout, obsidian-web-mcp, etc.).

## TL;DR

```python
# BEFORE
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("my-service")

# AFTER
from workos_shared.logger import get_logger
logger = get_logger(__name__, service_name="my-service")
```

Everything else — call sites like `logger.info(...)`, `logger.error(...)` — stays the same.

## Step-by-step

### 1. Install the shared package

Local dev:

```bash
pip install -e /path/to/workos-shared
```

VPS (per service):

```bash
# Clone once, reuse across services:
cd /home/ccuser
git clone git@github.com:greegorij/workos-shared.git  # or use https
# Then in each consumer's venv/uv env:
cd /home/ccuser/jarvis-rag
uv pip install -e /home/ccuser/workos-shared
```

Optionally add to the consumer's `pyproject.toml` dependencies once stable:

```toml
dependencies = [
    "workos-shared @ file:///home/ccuser/workos-shared",
    # ...
]
```

### 2. Swap the import

- Delete `logging.basicConfig(...)` calls — the shared logger handles it.
- Replace `logger = logging.getLogger("hardcoded-service-name")` with:

```python
from workos_shared.logger import get_logger
logger = get_logger(__name__, service_name="my-service")
```

Use `__name__` (module dotted path) for granularity, and set `service_name=` to match systemd unit name (makes journalctl filtering obvious).

### 3. Keep existing custom handlers

If the service adds syslog / Sentry / anything else, pass them in:

```python
import logging.handlers
import sentry_sdk  # example

syslog = logging.handlers.SysLogHandler(address="/dev/log")
logger = get_logger(
    __name__,
    service_name="my-service",
    extra_handlers=[syslog],
)
```

The shared logger is additive — it attaches console + rotating file + optional webhook, and your handlers are added on top.

### 4. Configure via env (systemd `EnvironmentFile=`)

| Variable | Purpose | Example |
|---|---|---|
| `LOG_LEVEL` | Overrides the `level=` argument. | `LOG_LEVEL=DEBUG` |
| `LOG_DIR` | Rotating file directory. Created if missing. | `LOG_DIR=/var/log/jarvis` |
| `JARVIS_SERVICE` | Fallback service name when `service_name` arg is omitted. | `JARVIS_SERVICE=jarvis-rag` |
| `WEBHOOK_URL` | If set, ERROR+ records are POSTed as JSON. | `WEBHOOK_URL=https://hooks.slack.com/...` |

Add these to `/home/ccuser/.{service}.env` (never inline in `Environment=` — follows the Jarvis secrets policy).

### 5. Smoke test

```bash
sudo systemctl restart my-service
journalctl -u my-service -n 30 --no-pager
```

Expect JSON-per-line entries:

```json
{"timestamp":"2026-04-21T10:55:03+0000","level":"INFO","service":"jarvis-rag","logger":"jarvis_rag.server","message":"started","context":{"port":8765}}
```

If you get plain text, either `structured=False` was passed or the logger is a legacy one still using `logging.basicConfig`.

### 6. Rollback

The change is purely additive (handlers + formatter), no behavioural side effects. Roll back with `git revert` of the two-line diff and restart the service.

## Repos on the backlog

| Repo | Modules to migrate first | Notes |
|---|---|---|
| `vault-mcp` (obsidian-web-mcp VPS mirror) | `server`, `oauth` | Mirrors `obsidian-web-mcp`; migrate together to avoid drift. |
| `scout` | `core/config`, `sources/*`, `agents/*` | Uses Protocol-based DI — `extra_handlers=` handles anything custom. |
| `jarvis-vps/jobs/*` | `mail_digest`, `pm_alert`, etc. | Short-lived cron jobs — pass `log_file=False` if `LOG_DIR` not writable. |
| `budget-agent` | `server` | Already structured-ish; trivial swap. |
| `social-agents` (standalone) | `linkedin_sniffer`, `fb_sniffer` | Decide SSOT (s84 audit flagged drift vs `jarvis-rag/src/social_agents/`). |

## Checklist for each new adopter

- [ ] `pip install -e /home/ccuser/workos-shared` in consumer's venv
- [ ] Replace `logging.getLogger` + `basicConfig` with `get_logger`
- [ ] Keep any custom handlers via `extra_handlers=`
- [ ] Add `LOG_LEVEL` / `WEBHOOK_URL` to `.env` if needed
- [ ] Smoke test: `systemctl restart` + `journalctl -u … -n 30`
- [ ] Commit per module (small, reviewable scope)
