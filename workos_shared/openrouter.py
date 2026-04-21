"""workos_shared.openrouter — stdlib-only OpenRouter API client (WorkOS E2 S6).

OpenRouter is a unified gateway to many LLM providers (Claude, GPT, Gemini,
open-source models). Useful for:
    - Experimentation with models other than Claude (dev / testing)
    - Safety net when Anthropic Max x20 weekly quota is exhausted
    - Multi-provider resilience for night-time Cloud Routines (E2 S6 WorkOS)

This is a stdlib-only wrapper (no `requests` / `httpx` dependency) — consistent
with workos-shared zero-external-deps policy. All HTTP via `urllib.request`.

API key resolution order:
    1. Explicit `api_key` argument to OpenRouterClient(...)
    2. Environment variable OPENROUTER_API_KEY
    3. File at ~/.claude/.openrouter.env (first line, or OPENROUTER_API_KEY=...)

Usage::

    from workos_shared.openrouter import OpenRouterClient
    client = OpenRouterClient()  # reads env/file automatically
    response = client.chat(
        model="google/gemini-2.5-flash",
        messages=[{"role": "user", "content": "Hi"}],
    )
    print(response["choices"][0]["message"]["content"])

Design notes (s87 E2 S6 stub):
    - `list_models()` caches in-memory for 1h (avoid repeated GET).
    - HTTP errors raise OpenRouterError with status code + body.
    - Timeouts default 60s (configurable).
    - Logging via workos_shared.logger (get_logger is optional).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from workos_shared.logger import get_logger

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_TIMEOUT_SECONDS = 60
_MODELS_CACHE_TTL_SECONDS = 3600  # 1h

logger = get_logger(__name__, service_name="workos-openrouter")


class OpenRouterError(Exception):
    """Raised for HTTP errors from OpenRouter API."""

    def __init__(self, status_code: int, body: str, message: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"OpenRouter HTTP {status_code}: {body[:200]}")


def _resolve_api_key(explicit_key: str | None) -> str:
    """Return API key from explicit argument, env var, or ~/.claude/.openrouter.env file.

    Raises ValueError if no key found.
    """
    if explicit_key:
        return explicit_key

    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        return env_key

    env_file = Path.home() / ".claude" / ".openrouter.env"
    if env_file.exists():
        content = env_file.read_text().strip()
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        if content and "=" not in content:
            return content

    raise ValueError(
        "OpenRouter API key not found. Provide `api_key=...`, set "
        "OPENROUTER_API_KEY env var, or create ~/.claude/.openrouter.env"
    )


class OpenRouterClient:
    """Thin wrapper over OpenRouter REST API (OpenAI-compatible schema).

    Parameters
    ----------
    api_key:
        Explicit API key. If None, resolved from env/file (see module docstring).
    base_url:
        Override base URL (default: https://openrouter.ai/api/v1).
    timeout:
        HTTP timeout in seconds (default 60).
    app_name:
        Optional X-Title header (helps OpenRouter dashboard identify traffic).
    app_url:
        Optional HTTP-Referer header (OpenRouter analytics).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: int = _DEFAULT_TIMEOUT_SECONDS,
        app_name: str | None = None,
        app_url: str | None = None,
    ):
        self.api_key = _resolve_api_key(api_key)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.app_name = app_name
        self.app_url = app_url
        self._models_cache: dict[str, Any] | None = None
        self._models_cache_ts: float = 0.0

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.app_url:
            headers["HTTP-Referer"] = self.app_url
        if self.app_name:
            headers["X-Title"] = self.app_name
        return headers

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "openrouter_http_error",
                extra={"context": {"status": exc.code, "path": path, "body_preview": body[:200]}},
            )
            raise OpenRouterError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            logger.error("openrouter_network_error", extra={"context": {"path": path, "reason": str(exc.reason)}})
            raise

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run a chat completion (OpenAI-compatible schema).

        Parameters
        ----------
        model:
            Model identifier (e.g. ``"google/gemini-2.5-flash"``, ``"anthropic/claude-4-6-sonnet"``).
        messages:
            List of ``{"role": "...", "content": "..."}`` dicts.
        **kwargs:
            Additional OpenRouter parameters (``temperature``, ``max_tokens``,
            ``top_p``, ``stream``, ``tools``, etc.). Passed verbatim.

        Returns
        -------
        dict
            Full OpenRouter response payload including ``choices``, ``usage``, etc.
        """
        payload: dict[str, Any] = {"model": model, "messages": messages, **kwargs}
        logger.debug("openrouter_chat", extra={"context": {"model": model, "messages_count": len(messages)}})
        return self._request("POST", "/chat/completions", payload)

    def list_models(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """List available OpenRouter models. Result cached in memory for 1h.

        Parameters
        ----------
        force_refresh:
            If True, bypass cache and re-fetch.

        Returns
        -------
        list of dict
            OpenRouter ``data`` array (each item = model info with id, pricing, context_length).
        """
        now = time.time()
        if (
            not force_refresh
            and self._models_cache is not None
            and (now - self._models_cache_ts) < _MODELS_CACHE_TTL_SECONDS
        ):
            return self._models_cache  # type: ignore[return-value]

        response = self._request("GET", "/models")
        models = response.get("data", [])
        self._models_cache = models
        self._models_cache_ts = now
        return models
