"""Structured logger for the Jarvis ecosystem.

Design goals (WorkOS E3 S2):
- Zero external dependencies (stdlib only).
- JSON-per-line by default (VPS services → journalctl / log aggregators).
- Rotating file handler + console handler out of the box.
- Optional webhook dispatch for ERROR+ (Slack / Telegram / PagerDuty later).
- Opt-in; consumers can still attach their own handlers (Sentry, syslog, etc.).

Entry point: :func:`get_logger`.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "get_logger",
    "JsonFormatter",
    "WebhookHandler",
    "DEFAULT_LOG_DIR",
]

DEFAULT_LOG_DIR = "/tmp/workos-logs"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB per log file
_BACKUP_COUNT = 5

# Keys to pull from LogRecord.__dict__ into the JSON payload when present.
_STANDARD_RECORD_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON.

    Payload: timestamp (ISO-8601 UTC), level, name, message, context (dict).
    Any `extra={"context": {...}}` dict is merged under ``context``.
    Any other non-standard attributes added via ``extra=`` are collected under
    ``context`` too (so callers can use either pattern).
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }

        context: dict[str, Any] = {}
        # Explicit context dict wins.
        explicit_ctx = record.__dict__.get("context")
        if isinstance(explicit_ctx, dict):
            context.update(explicit_ctx)

        # Also absorb any stray extras (excluding stdlib keys + the context key itself).
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key == "context":
                continue
            # Skip private logging internals.
            if key.startswith("_"):
                continue
            context[key] = value

        if context:
            payload["context"] = context

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # Fallback: coerce everything to strings — logging should never blow up.
            safe = {k: str(v) for k, v in payload.items()}
            return json.dumps(safe, ensure_ascii=False)


class WebhookHandler(logging.Handler):
    """POST ERROR+ log records as JSON to a webhook URL.

    Fire-and-forget, non-blocking (dispatched in a worker thread). Failures are
    swallowed so logging never breaks the caller. Intended for ERROR/CRITICAL
    alerts — set ``level=logging.ERROR`` on the handler (default here).
    """

    def __init__(self, url: str, service_name: str, timeout: float = 3.0) -> None:
        super().__init__(level=logging.ERROR)
        self.url = url
        self.service_name = service_name
        self.timeout = timeout
        self.setFormatter(JsonFormatter(service_name=service_name))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            body = self.format(record).encode("utf-8")
        except Exception:  # pragma: no cover — formatter already has fallback
            return

        def _send() -> None:
            req = urllib.request.Request(
                self.url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    pass
            except (urllib.error.URLError, OSError, TimeoutError):
                # Swallow — logger must never break the caller.
                pass

        threading.Thread(target=_send, daemon=True).start()


# Track loggers we have already configured, keyed by (name, service_name) so
# that tests with different service_name stay isolated but repeated calls in
# production are idempotent.
_configured: set[tuple[str, str]] = set()
_configure_lock = threading.Lock()


def _resolve_log_path(service_name: str) -> Path:
    base = Path(os.environ.get("LOG_DIR", DEFAULT_LOG_DIR))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{service_name}.log"


def _level_from_str(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(level.upper())
    if not isinstance(resolved, int):
        raise ValueError(f"Unknown log level: {level!r}")
    return resolved


def get_logger(
    name: str,
    *,
    service_name: str | None = None,
    level: str | int = "INFO",
    structured: bool = True,
    extra_handlers: Iterable[logging.Handler] | None = None,
    webhook_url: str | None = None,
    log_file: bool = True,
) -> logging.Logger:
    """Return a configured :class:`logging.Logger`.

    Args:
        name: Logger name (typically ``__name__``).
        service_name: Service identifier used in JSON payload + log filename.
            Defaults to env ``JARVIS_SERVICE`` or, failing that, the root of
            ``name`` (e.g. ``"jarvis_rag"`` for ``"jarvis_rag.indexer"``).
        level: Log level — ``"DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"``
            or an int. Overridden by env ``LOG_LEVEL`` when set.
        structured: ``True`` (default) → JSON formatter. ``False`` → plain text
            formatter (dev mode).
        extra_handlers: Optional iterable of extra handlers to attach (Sentry,
            syslog, etc.). Added in addition to console + file + webhook.
        webhook_url: Override the ``WEBHOOK_URL`` env var. If neither is set,
            no webhook is attached.
        log_file: If False, skip the rotating file handler (useful in tests
            or short-lived scripts).

    Returns:
        A ``logging.Logger`` ready to use. Safe to call repeatedly — handlers
        are configured exactly once per (name, service_name) pair.
    """
    # Env precedence for level matches the rest of the Jarvis ecosystem.
    env_level = os.environ.get("LOG_LEVEL")
    effective_level = _level_from_str(env_level) if env_level else _level_from_str(level)

    if service_name is None:
        service_name = os.environ.get("JARVIS_SERVICE") or name.split(".")[0] or "workos"

    logger = logging.getLogger(name)
    logger.setLevel(effective_level)
    # Prevent double-emission if a parent (root) logger also has handlers.
    logger.propagate = False

    key = (name, service_name)
    with _configure_lock:
        if key in _configured:
            return logger

        if structured:
            formatter: logging.Formatter = JsonFormatter(service_name=service_name)
        else:
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )

        # Console — always on (systemd journalctl captures STDOUT).
        console = logging.StreamHandler(stream=sys.stdout)
        console.setLevel(effective_level)
        console.setFormatter(formatter)
        logger.addHandler(console)

        # Rotating file — on by default, opt-out for tests.
        if log_file:
            try:
                log_path = _resolve_log_path(service_name)
                file_handler = RotatingFileHandler(
                    log_path,
                    maxBytes=_MAX_BYTES,
                    backupCount=_BACKUP_COUNT,
                    encoding="utf-8",
                )
                file_handler.setLevel(effective_level)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except OSError:
                # If we can't write logs to disk, degrade gracefully to
                # console-only. Logger must never break the caller.
                logger.warning(
                    "workos_shared.logger: file handler disabled (permission denied)"
                )

        # Optional webhook (ERROR+).
        resolved_webhook = webhook_url or os.environ.get("WEBHOOK_URL")
        if resolved_webhook:
            logger.addHandler(WebhookHandler(resolved_webhook, service_name=service_name))

        # Consumer-provided handlers (Sentry, syslog, etc.).
        if extra_handlers:
            for handler in extra_handlers:
                logger.addHandler(handler)

        _configured.add(key)

    return logger
