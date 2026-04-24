"""Anthropic API wrapper with Batch API + long-prompt routing.

Design goals (WorkOS E16 S2):
- Batch API by default (50% cheaper, ~1-5 min latency)
- Auto-fallback to sync API on batch timeout or failure
- Long-prompt detection → switch to Sonnet 4.5 with 1M context beta + streaming
- Zero impact on callers that use sync API directly (wrapper is opt-in)
- Lazy import of `anthropic` SDK — the package is an optional extra

Consumers (post-E16 S2): fireflies_agent, vault_keeper (semantic), budget_agent.

Reference implementation extracted from fireflies_agent/agent.py:_call_claude
(PR #3/#4 s78-s80). Vault Keeper used a stripped-down variant without
long-prompt handling → fails at 200K token ceiling (incident s92).

Install with anthropic extra::

    pip install workos-shared[anthropic]

Or rely on the consumer repo already pinning `anthropic` — this module only
imports it lazily inside the relevant call paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import Anthropic

__all__ = [
    "CallResult",
    "call_claude",
    "call_claude_async",
    "detect_long_prompt",
    "parse_json_response",
    "AnthropicClientError",
    "LONG_PROMPT_CHAR_THRESHOLD",
    "DEFAULT_MODEL",
    "LONG_CONTEXT_MODEL",
    "LONG_CONTEXT_BETA_HEADER",
]

# --- Constants (override via call-site kwargs) -------------------------------

# Polish text ~3.04 chars/token (measured 2026-04-21 FF audit: 617K chars =
# 203K tokens). Using 3.0 chars/token with 20K token headroom from the 200K
# Sonnet 4 ceiling → 540K chars safe ceiling before switching to 1M context.
LONG_PROMPT_CHAR_THRESHOLD = 540_000

DEFAULT_MODEL = "claude-sonnet-4-20250514"
LONG_CONTEXT_MODEL = "claude-sonnet-4-5-20250929"
LONG_CONTEXT_BETA_HEADER = "context-1m-2025-08-07"

_BATCH_POLL_INTERVAL_S = 30
_BATCH_MAX_POLLS = 60  # 60 * 30s = 30 min ceiling
_DEFAULT_MAX_TOKENS = 8192
_LONG_CONTEXT_MAX_TOKENS = 32768
_SYNC_FALLBACK_MAX_TOKENS = 4096

_logger = logging.getLogger(__name__)


class AnthropicClientError(RuntimeError):
    """Raised when all call paths (batch + sync) fail."""


@dataclass
class CallResult:
    """Outcome of a Claude invocation.

    Attributes:
        text: Model-generated text, stripped of leading/trailing whitespace.
        model: Model identifier actually used (may differ from request when
            long-prompt routing kicks in).
        path: Which code path produced the result — ``"batch"``, ``"sync"``,
            or ``"stream"`` (long-context).
        elapsed_s: Wall-clock seconds from call start to result.
    """

    text: str
    model: str
    path: Literal["batch", "sync", "stream"]
    elapsed_s: float


# --- Helpers ----------------------------------------------------------------


def detect_long_prompt(
    *parts: str,
    threshold: int = LONG_PROMPT_CHAR_THRESHOLD,
) -> bool:
    """Return True if total char length of *parts* exceeds *threshold*.

    Pass system + user strings (and any other large payload parts) — the
    function sums their ``len()``. Thresholds default to the empirically
    measured PL safety ceiling (see module docstring).
    """
    total = sum(len(p) for p in parts)
    return total > threshold


def parse_json_response(text: str) -> dict | None:
    """Parse Claude JSON response, tolerating markdown code fences.

    Returns the decoded dict or ``None`` if the payload is malformed. Does
    not raise — callers inspect the return value. Log line is emitted at
    ERROR level when parsing fails (with truncated preview).
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop first fence line.
        parts = stripped.split("\n", 1)
        stripped = parts[1] if len(parts) > 1 else ""
        # Claude sometimes opens with ```json — drop the language tag.
        if stripped.startswith("json\n"):
            stripped = stripped[5:]
        elif stripped.startswith("json"):
            stripped = stripped[4:].lstrip()
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()

    try:
        result = json.loads(stripped)
    except json.JSONDecodeError as exc:
        _logger.error(
            "anthropic_client: invalid JSON response (%s): %r", exc, stripped[:300]
        )
        return None

    if not isinstance(result, dict):
        _logger.error(
            "anthropic_client: JSON response is not an object: %r", stripped[:300]
        )
        return None
    return result


# --- Internal routing -------------------------------------------------------


def _import_anthropic():
    try:
        import anthropic  # noqa: F401

        return anthropic
    except ImportError as exc:  # pragma: no cover — tested via monkeypatch
        raise AnthropicClientError(
            "workos_shared.anthropic_client requires the `anthropic` package. "
            "Install with `pip install workos-shared[anthropic]` or add "
            "`anthropic` to the consumer repo dependencies."
        ) from exc


def _pick_model_and_headers(
    long_prompt: bool,
    *,
    default_model: str,
    long_context_model: str,
) -> tuple[str, dict[str, str]]:
    if long_prompt:
        return long_context_model, {"anthropic-beta": LONG_CONTEXT_BETA_HEADER}
    return default_model, {}


def _streaming_sync_call(
    client: Anthropic,
    *,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int,
    extra_headers: dict[str, str],
) -> str:
    text_parts: list[str] = []
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
        extra_headers=extra_headers if extra_headers else None,
    ) as stream:
        for chunk in stream.text_stream:
            text_parts.append(chunk)
    return "".join(text_parts).strip()


def _plain_sync_call(
    client: Anthropic,
    *,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int,
) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text.strip()


def _run_batch(
    client: Anthropic,
    *,
    custom_id: str,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int,
    poll_interval: int,
    max_polls: int,
    sleep: Any,
) -> str | None:
    """Run via Batch API, return text on success or None on timeout/failure."""
    batch = client.messages.batches.create(
        requests=[
            {
                "custom_id": custom_id,
                "params": {
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user_message}],
                },
            }
        ]
    )
    _logger.info("anthropic_client: batch created id=%s", batch.id)

    for attempt in range(max_polls):
        sleep_result = sleep(poll_interval)
        if asyncio.iscoroutine(sleep_result):  # pragma: no cover — async path
            raise RuntimeError(
                "anthropic_client._run_batch was passed an async sleep; use "
                "call_claude_async instead"
            )
        status = client.messages.batches.retrieve(batch.id)
        if status.processing_status == "ended":
            results = list(client.messages.batches.results(batch.id))
            if results and results[0].result.type == "succeeded":
                elapsed = (attempt + 1) * poll_interval
                _logger.info(
                    "anthropic_client: batch succeeded id=%s after %ds",
                    batch.id,
                    elapsed,
                )
                return results[0].result.message.content[0].text.strip()
            error = results[0].result if results else "no-results"
            _logger.error("anthropic_client: batch failed id=%s reason=%r", batch.id, error)
            return None

    _logger.warning(
        "anthropic_client: batch timed out id=%s after %d polls (%ds)",
        batch.id,
        max_polls,
        max_polls * poll_interval,
    )
    return None


async def _run_batch_async(
    client: Anthropic,
    *,
    custom_id: str,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int,
    poll_interval: int,
    max_polls: int,
) -> str | None:
    batch = client.messages.batches.create(
        requests=[
            {
                "custom_id": custom_id,
                "params": {
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user_message}],
                },
            }
        ]
    )
    _logger.info("anthropic_client: batch created id=%s", batch.id)

    for attempt in range(max_polls):
        await asyncio.sleep(poll_interval)
        status = client.messages.batches.retrieve(batch.id)
        if status.processing_status == "ended":
            results = list(client.messages.batches.results(batch.id))
            if results and results[0].result.type == "succeeded":
                elapsed = (attempt + 1) * poll_interval
                _logger.info(
                    "anthropic_client: batch succeeded id=%s after %ds",
                    batch.id,
                    elapsed,
                )
                return results[0].result.message.content[0].text.strip()
            error = results[0].result if results else "no-results"
            _logger.error("anthropic_client: batch failed id=%s reason=%r", batch.id, error)
            return None

    _logger.warning(
        "anthropic_client: batch timed out id=%s after %d polls (%ds)",
        batch.id,
        max_polls,
        max_polls * poll_interval,
    )
    return None


# --- Public entrypoints -----------------------------------------------------


def call_claude(
    *,
    api_key: str,
    system: str,
    user_message: str,
    custom_id: str,
    long_prompt_safe: bool = True,
    default_model: str = DEFAULT_MODEL,
    long_context_model: str = LONG_CONTEXT_MODEL,
    long_prompt_threshold: int = LONG_PROMPT_CHAR_THRESHOLD,
    batch_enabled: bool = True,
    batch_poll_interval_s: int = _BATCH_POLL_INTERVAL_S,
    batch_max_polls: int = _BATCH_MAX_POLLS,
    max_tokens_short: int = _DEFAULT_MAX_TOKENS,
    max_tokens_long: int = _LONG_CONTEXT_MAX_TOKENS,
    max_tokens_sync_fallback: int = _SYNC_FALLBACK_MAX_TOKENS,
    _client: Anthropic | None = None,
    _sleep: Any = time.sleep,
) -> CallResult:
    """Invoke Claude with Batch → sync fallback + long-prompt routing.

    Routing decision tree:
        1. If ``long_prompt_safe`` AND total prompt length > threshold:
           → long-context model + streaming sync call (skip Batch).
        2. Else if ``batch_enabled``: try Batch API (poll up to ~30 min).
           → on success: return CallResult(path="batch").
           → on timeout/failure: fall through to sync API.
        3. Fallback: plain sync API.

    Arguments match the behaviour of ``fireflies_agent._call_claude`` so that
    migrating consumers is a mechanical swap. ``_client`` and ``_sleep`` are
    test seams — production callers omit them.

    Raises:
        AnthropicClientError: All paths failed, including sync fallback.
    """
    anthropic_mod = _import_anthropic() if _client is None else None
    start = time.monotonic()

    long_prompt = long_prompt_safe and detect_long_prompt(
        system, user_message, threshold=long_prompt_threshold
    )
    model, extra_headers = _pick_model_and_headers(
        long_prompt,
        default_model=default_model,
        long_context_model=long_context_model,
    )

    if _client is not None:
        client = _client
    else:
        client = anthropic_mod.Anthropic(api_key=api_key)

    # --- Long-prompt path: streaming sync (skip Batch) ----------------------
    if long_prompt:
        _logger.info(
            "anthropic_client: long prompt %d chars → %s + 1M context streaming",
            len(system) + len(user_message),
            long_context_model,
        )
        try:
            text = _streaming_sync_call(
                client,
                model=model,
                system=system,
                user_message=user_message,
                max_tokens=max_tokens_long,
                extra_headers=extra_headers,
            )
            return CallResult(
                text=text,
                model=model,
                path="stream",
                elapsed_s=time.monotonic() - start,
            )
        except Exception as exc:
            raise AnthropicClientError(
                f"anthropic_client: long-prompt streaming failed: {exc}"
            ) from exc

    # --- Batch path (with fallback) -----------------------------------------
    if batch_enabled:
        try:
            batch_text = _run_batch(
                client,
                custom_id=custom_id,
                model=model,
                system=system,
                user_message=user_message,
                max_tokens=max_tokens_short,
                poll_interval=batch_poll_interval_s,
                max_polls=batch_max_polls,
                sleep=_sleep,
            )
        except Exception as exc:
            _logger.warning("anthropic_client: batch API error (%s), falling back to sync", exc)
            batch_text = None

        if batch_text is not None:
            return CallResult(
                text=batch_text,
                model=model,
                path="batch",
                elapsed_s=time.monotonic() - start,
            )

    # --- Sync fallback -------------------------------------------------------
    try:
        text = _plain_sync_call(
            client,
            model=model,
            system=system,
            user_message=user_message,
            max_tokens=max_tokens_sync_fallback,
        )
    except Exception as exc:
        raise AnthropicClientError(
            f"anthropic_client: sync fallback failed: {exc}"
        ) from exc

    return CallResult(
        text=text,
        model=model,
        path="sync",
        elapsed_s=time.monotonic() - start,
    )


async def call_claude_async(
    *,
    api_key: str,
    system: str,
    user_message: str,
    custom_id: str,
    long_prompt_safe: bool = True,
    default_model: str = DEFAULT_MODEL,
    long_context_model: str = LONG_CONTEXT_MODEL,
    long_prompt_threshold: int = LONG_PROMPT_CHAR_THRESHOLD,
    batch_enabled: bool = True,
    batch_poll_interval_s: int = _BATCH_POLL_INTERVAL_S,
    batch_max_polls: int = _BATCH_MAX_POLLS,
    max_tokens_short: int = _DEFAULT_MAX_TOKENS,
    max_tokens_long: int = _LONG_CONTEXT_MAX_TOKENS,
    max_tokens_sync_fallback: int = _SYNC_FALLBACK_MAX_TOKENS,
    _client: Anthropic | None = None,
) -> CallResult:
    """Async variant of :func:`call_claude`.

    Runs the sync Anthropic SDK calls inline (they are short-lived I/O
    relative to the 30s poll cadence) and uses ``asyncio.sleep`` between
    batch polls. Matches the existing async-flavoured pattern in
    ``fireflies_agent.agent._call_claude`` and
    ``vault_keeper.semantic.run_semantic_analysis``.
    """
    anthropic_mod = _import_anthropic() if _client is None else None
    start = time.monotonic()

    long_prompt = long_prompt_safe and detect_long_prompt(
        system, user_message, threshold=long_prompt_threshold
    )
    model, extra_headers = _pick_model_and_headers(
        long_prompt,
        default_model=default_model,
        long_context_model=long_context_model,
    )

    if _client is not None:
        client = _client
    else:
        client = anthropic_mod.Anthropic(api_key=api_key)

    if long_prompt:
        _logger.info(
            "anthropic_client: long prompt %d chars → %s + 1M context streaming",
            len(system) + len(user_message),
            long_context_model,
        )
        try:
            text = _streaming_sync_call(
                client,
                model=model,
                system=system,
                user_message=user_message,
                max_tokens=max_tokens_long,
                extra_headers=extra_headers,
            )
            return CallResult(
                text=text,
                model=model,
                path="stream",
                elapsed_s=time.monotonic() - start,
            )
        except Exception as exc:
            raise AnthropicClientError(
                f"anthropic_client: long-prompt streaming failed: {exc}"
            ) from exc

    if batch_enabled:
        try:
            batch_text = await _run_batch_async(
                client,
                custom_id=custom_id,
                model=model,
                system=system,
                user_message=user_message,
                max_tokens=max_tokens_short,
                poll_interval=batch_poll_interval_s,
                max_polls=batch_max_polls,
            )
        except Exception as exc:
            _logger.warning("anthropic_client: batch API error (%s), falling back to sync", exc)
            batch_text = None

        if batch_text is not None:
            return CallResult(
                text=batch_text,
                model=model,
                path="batch",
                elapsed_s=time.monotonic() - start,
            )

    try:
        text = _plain_sync_call(
            client,
            model=model,
            system=system,
            user_message=user_message,
            max_tokens=max_tokens_sync_fallback,
        )
    except Exception as exc:
        raise AnthropicClientError(
            f"anthropic_client: sync fallback failed: {exc}"
        ) from exc

    return CallResult(
        text=text,
        model=model,
        path="sync",
        elapsed_s=time.monotonic() - start,
    )
