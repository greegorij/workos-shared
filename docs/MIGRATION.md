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

---

# `workos_shared.openrouter` (v0.2.0 — WorkOS E2 S6, s87)

**Use case:** gateway to multiple LLM providers (Claude, GPT, Gemini, open-source) via OpenRouter. Useful for model experimentation, multi-provider resilience, Cloud Routine fallback when Anthropic weekly quota exhausted.

## Quickstart

```python
from workos_shared import OpenRouterClient

# Resolution order: explicit → OPENROUTER_API_KEY env → ~/.claude/.openrouter.env file
client = OpenRouterClient()

response = client.chat(
    model="google/gemini-2.5-flash",
    messages=[{"role": "user", "content": "Hello"}],
    temperature=0.7,
)
print(response["choices"][0]["message"]["content"])

# List available models (cached 1h)
models = client.list_models()
print([m["id"] for m in models[:5]])
```

## API key storage

Three resolution paths (in order):

1. **Explicit argument** — `OpenRouterClient(api_key="or-xxx")`
2. **Environment variable** — `OPENROUTER_API_KEY` in shell
3. **File** — `~/.claude/.openrouter.env` (either `OPENROUTER_API_KEY=or-xxx` line, or plain key only)

## Design notes

- **Zero external deps** — stdlib only (`urllib.request`), consistent with workos-shared policy.
- **Cache:** `list_models()` caches in memory for 1h. Pass `force_refresh=True` to bypass.
- **Timeout:** default 60s (configurable via `timeout=...`).
- **Error handling:** HTTP errors raise `OpenRouterError` with `.status_code` and `.body`. Network errors propagate `urllib.error.URLError`.
- **Logging:** all activity logged via `workos_shared.logger` (service name `workos-openrouter`).
- **Analytics:** pass `app_name=` / `app_url=` to OpenRouterClient for OpenRouter dashboard attribution (adds `X-Title` / `HTTP-Referer` headers).

## Testing

```bash
cd workos-shared
python3 -m pytest tests/test_openrouter.py -v
# 13 tests (key resolution, client init, headers, chat payload, list_models cache, error handling)
```

## Typical use cases (s87)

1. **Dev experimentation** — test prompts against Gemini before committing to Claude cost.
2. **Cloud Routine fallback** — when Anthropic Max x20 weekly quota exhausted, switch Supervisor to Gemini via OpenRouter.
3. **Multi-model evaluation** — run same eval against 3 models, compare quality/cost.
4. **VPS Gemini army (E2 S6 future)** — VPS subagents use OpenRouter to route to Gemini API (batch discount 50%).

## Not yet implemented (E2 S6 backlog)

- Streaming responses (`stream=True`) — currently returns full response only.
- Tool use / function calling support (OpenRouter schema = OpenAI-compatible tools).
- Retry with exponential backoff (consumer should wrap if needed).
- Per-model cost tracking integration with E4 tracker.

---

# `workos_shared.anthropic_client` (v0.3.0 — WorkOS E16 S2)

**Use case:** consolidated Anthropic wrapper shared across `fireflies_agent`, `vault_keeper`, `budget_agent`. Solves the drift where Fireflies had long-prompt handling (PR #3/#4) but Vault Keeper did not → VK semantic crashed on 200K token ceiling (incident s92 2026-04-23).

## Quickstart

```python
from workos_shared import call_claude

result = call_claude(
    api_key=ANTHROPIC_API_KEY,
    system=SYSTEM_PROMPT,
    user_message=user_text,
    custom_id=f"myservice-{unique_id}",
    long_prompt_safe=True,
    batch_enabled=True,
)

if result.text.startswith("```"):
    from workos_shared import parse_json_response
    data = parse_json_response(result.text)  # tolerates markdown fences
```

Async variant (for `async def` call sites):

```python
from workos_shared import call_claude_async

result = await call_claude_async(
    api_key=ANTHROPIC_API_KEY,
    system=SYSTEM_PROMPT,
    user_message=user_text,
    custom_id="myservice-1",
)
```

## Migration — replace raw `anthropic.Anthropic(...)` calls

### 1. Install the `anthropic` extra

```bash
# Consumer repo pyproject.toml:
dependencies = [
    "workos-shared[anthropic] @ file:///home/ccuser/workos-shared",
    # ...
]
```

Or rely on the consumer already pinning `anthropic` directly — the wrapper
imports it lazily.

### 2. Swap the 70-line Batch + sync fallback block

**Before** (fragment from `vault_keeper/semantic.py` pre-E16):

```python
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
try:
    batch = client.messages.batches.create(requests=[...])
    for attempt in range(60):
        await asyncio.sleep(30)
        status = client.messages.batches.retrieve(batch.id)
        if status.processing_status == "ended":
            # ... 20 more lines
except Exception:
    ...
response = client.messages.create(...)  # sync fallback
text = response.content[0].text.strip()
return _parse_response(text)
```

**After:**

```python
from workos_shared import call_claude_async, parse_json_response

result = await call_claude_async(
    api_key=ANTHROPIC_API_KEY,
    system=SYSTEM_PROMPT,
    user_message=user_message,
    custom_id="vault-keeper-semantic",
    long_prompt_safe=True,   # unlocks 1M context streaming path when needed
)
return parse_json_response(result.text)
```

### 3. Decision tree the wrapper encodes

```
long_prompt_safe = True AND len(system)+len(user) > 540_000
    └── YES → Sonnet 4.5 + 1M context beta + streaming sync
    └── NO  → try Batch API (poll up to ~30 min)
              └── success → CallResult(path="batch")
              └── timeout/error → sync API fallback (path="sync")
```

`CallResult.path` tells you which branch ran — log it to journalctl for
pipeline observability.

## Configuration knobs

| Argument | Default | When to tune |
|---|---|---|
| `long_prompt_safe` | `True` | Disable only if you're certain the prompt is bounded and want stricter latency. |
| `long_prompt_threshold` | `540_000` | Measured for Polish text (3.04 chars/token + 20K headroom). Lower for other languages with better tokenisation (e.g. English: try `600_000`). |
| `batch_enabled` | `True` | Disable for hot interactive paths where 30s poll floor is unacceptable. |
| `batch_poll_interval_s` | `30` | Lower for test suites; don't drop below 15 in prod (Anthropic rate limits batch polling). |
| `batch_max_polls` | `60` | 60 × 30s = 30 min. Bump if your workload expects long batches; lower if you want faster sync fallback. |
| `default_model` | `claude-sonnet-4-20250514` | Bump to latest Sonnet when released. |
| `long_context_model` | `claude-sonnet-4-5-20250929` | 1M context beta variant. |

## Testing

```bash
cd workos-shared
python3 -m pytest tests/test_anthropic_client.py -v
# 8 tests: threshold, JSON parsing (plain + fenced + malformed), batch success,
# timeout→sync fallback, long-prompt streaming, dual failure raises, async batch,
# batch_enabled=False bypass.
```

## Known gaps / future work

- **No rate-limit backoff on batch polling.** Anthropic hasn't surfaced 429 on
  `batches.retrieve()` in practice, but if that changes add tenacity here
  without touching consumers.
- **No streaming support for non-long prompts** — Batch path returns full text.
  Adding a `stream=True` arg for the sync path is trivial if a consumer needs
  token-by-token output.
- **Cost tracking integration with E4 tracker** — follow-up sprint.

---

# `workos_shared.webhook` (v0.3.0 — WorkOS E16 S2)

**Use case:** HMAC signature verification + dedup helpers shared between
`fireflies_agent` (full) and `jarvis_rag` (currently missing dedup — adoption
closes the gap identified in the E16 S1 audit).

## Quickstart

```python
from workos_shared import verify_hmac_signature, PersistentDedup, SignatureMismatch

# Per-request: verify HMAC
try:
    verify_hmac_signature(
        body=raw_body_bytes,
        signature=request.headers.get("x-hub-signature", ""),
        secret=FIREFLIES_WEBHOOK_SECRET,
    )
except SignatureMismatch as exc:
    return PlainTextResponse(str(exc), status_code=403)

# At startup: load dedup (loads existing IDs from disk)
dedup = PersistentDedup("/home/ccuser/jarvis-rag/.processed_ids")

# Per-request: dedup + process
if not dedup.add(meeting_id):
    return PlainTextResponse("already processed", status_code=200)
asyncio.create_task(process(meeting_id))
```

## Migration — swap ad-hoc HMAC blocks

**Before** (fragment from `jarvis_rag/server.py` pre-E16):

```python
signature = request.headers.get("X-Hub-Signature-256", "").removeprefix("sha256=")
expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(signature, expected):
    return PlainTextResponse("Invalid signature", status_code=403)
```

**After:**

```python
from workos_shared import verify_hmac_signature, SignatureMismatch

try:
    verify_hmac_signature(
        body=body,
        signature=request.headers.get("X-Hub-Signature-256", ""),
        secret=SECRET,
    )
except SignatureMismatch:
    return PlainTextResponse("Invalid signature", status_code=403)
```

The wrapper is case-insensitive on the algo prefix (`sha256=` / `SHA256=`) and
tolerates bare hex digests, so drop-in replacement works for both the Fireflies
scheme (`x-hub-signature: sha256=...`) and GitHub-style
(`X-Hub-Signature-256: sha256=...`).

## Why PersistentDedup beats an in-memory `set()`

Fireflies sends multiple webhooks per meeting (transcription completed +
summary completed). Without persistent dedup, a service restart between the
two webhooks re-processes the meeting. The file-backed set survives restarts
while staying in-memory O(1) at request time.

Append-only on success: the `add()` call writes one line, then cache entry.
`discard()` rewrites in-memory only (use after a processing failure to allow
retry on next restart).

## Testing

```bash
cd workos-shared
python3 -m pytest tests/test_webhook.py -v
# 13 tests: HMAC (scheme prefix, bare hex, case-insensitive, wrong digest,
# empty inputs, algo_hint override); PersistentDedup (add/dedupe/persist,
# restart survival, discard memory-only, purge destructive, empty key guard).
```

## Known gaps / future work

- **No timestamp replay guard.** If Fireflies adds a `X-Hub-Timestamp` header,
  a `max_age_seconds` param here would let us reject stale replays beyond HMAC.
- **No streaming-body verification.** Caller must read full body before
  calling — matches Fireflies/GitHub semantics, but wouldn't work for
  chunked-transfer sources if we adopt any.
