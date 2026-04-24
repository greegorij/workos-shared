"""Webhook helpers — HMAC signature verification + persistent dedup.

Design goals (WorkOS E16 S2):
- Stdlib-only (hashlib, hmac, pathlib, threading).
- Framework-agnostic: callers pass raw body + signature header; we don't import
  starlette/fastapi/flask. This keeps the module reusable across web stacks.
- HMAC constant-time comparison (``hmac.compare_digest``).
- Case-insensitive and scheme-tolerant signature parsing — Fireflies sends
  ``x-hub-signature: sha256=<hex>`` while GitHub-style sources often send
  ``X-Hub-Signature-256: sha256=<hex>``. Both work out of the box.
- Thread-safe persistent dedup (append-only file + in-memory cache, locked).

Consumers (post-E16 S2): fireflies_agent (full: HMAC + dedup),
jarvis_rag (HMAC + dedup to close the missing-dedup gap found in S1 audit).
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from pathlib import Path
from typing import Iterable

__all__ = [
    "SignatureMismatch",
    "verify_hmac_signature",
    "PersistentDedup",
]


class SignatureMismatch(Exception):
    """Raised when HMAC verification fails (or signature is missing/malformed)."""


# --- HMAC verification ------------------------------------------------------

_SUPPORTED_ALGOS: dict[str, str] = {
    "sha256": "sha256",
    "sha1": "sha1",
    "sha512": "sha512",
}


def _parse_signature(raw: str) -> tuple[str, str]:
    """Parse ``sha256=<hex>`` or bare hex. Returns (algo, hex).

    Raises ``SignatureMismatch`` on malformed input.
    """
    if not raw:
        raise SignatureMismatch("signature header is empty")

    value = raw.strip()
    if "=" in value:
        algo, _, digest = value.partition("=")
        algo = algo.strip().lower()
        digest = digest.strip()
    else:
        algo = "sha256"
        digest = value

    if algo not in _SUPPORTED_ALGOS:
        raise SignatureMismatch(f"unsupported algorithm: {algo!r}")

    if not digest:
        raise SignatureMismatch("signature digest is empty")

    return _SUPPORTED_ALGOS[algo], digest


def verify_hmac_signature(
    *,
    body: bytes,
    signature: str,
    secret: str,
    algo_hint: str | None = None,
) -> None:
    """Verify an HMAC signature over *body* using *secret*.

    Args:
        body: Raw request body (bytes). Caller is responsible for reading it
            from the framework request before any JSON parsing.
        signature: Header value — ``sha256=<hex>``, ``sha1=<hex>``, or bare hex.
        secret: Shared secret used to compute the expected HMAC.
        algo_hint: Force algorithm (``"sha256"``/``"sha1"``/``"sha512"``) and
            ignore the scheme prefix. Useful when the source always uses a
            fixed algo and you want to reject attempts to downgrade.

    Raises:
        SignatureMismatch: If the signature is missing, malformed, uses an
            unsupported algorithm, or does not match the expected digest.

    Returns:
        ``None`` on success (idiomatic: *absence of exception = OK*). Callers
        can catch the single exception type and convert it to an HTTP 403.
    """
    if not secret:
        raise SignatureMismatch("secret is empty — refuse to verify")

    algo_name, provided_hex = _parse_signature(signature)
    if algo_hint:
        requested = algo_hint.lower()
        if requested not in _SUPPORTED_ALGOS:
            raise SignatureMismatch(f"unsupported algo_hint: {algo_hint!r}")
        algo_name = _SUPPORTED_ALGOS[requested]

    expected_hex = hmac.new(
        secret.encode("utf-8"),
        body,
        getattr(hashlib, algo_name),
    ).hexdigest()

    if not hmac.compare_digest(expected_hex, provided_hex):
        raise SignatureMismatch("signature does not match expected digest")


# --- Persistent dedup -------------------------------------------------------


class PersistentDedup:
    """File-backed set of already-seen IDs with in-memory cache.

    Append-only: once an ID is added it stays on disk. Survives service
    restarts. Intended for webhook deduplication (Fireflies sends multiple
    webhooks per meeting — transcription + summary — we must process only one).

    Thread-safe via an internal lock. ``contains`` is O(1) against the cache,
    ``add`` appends one line to the file.

    Attributes:
        path: Filesystem path backing the dedup set.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: set[str] = self._load()

    # -- Internal --------------------------------------------------------

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return set()
        return {line.strip() for line in raw.splitlines() if line.strip()}

    # -- Public API ------------------------------------------------------

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._cache

    def contains(self, key: str) -> bool:
        """Return True if *key* was previously recorded."""
        return key in self

    def add(self, key: str) -> bool:
        """Record *key* as seen. Returns True if newly added, False if dupe.

        The underlying file is created lazily. Parent directories are created
        as needed.
        """
        if not key:
            raise ValueError("dedup key must be non-empty")

        with self._lock:
            if key in self._cache:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"{key}\n")
            self._cache.add(key)
            return True

    def discard(self, key: str) -> None:
        """Remove *key* from the in-memory cache only.

        Useful when processing fails and the service wants to allow a retry
        after restart. Does NOT rewrite the file — the on-disk record persists
        (clearing it requires a manual file edit or :meth:`purge`).
        """
        with self._lock:
            self._cache.discard(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __iter__(self) -> Iterable[str]:
        with self._lock:
            # Snapshot so iteration doesn't hold the lock.
            snapshot = tuple(self._cache)
        return iter(snapshot)

    def purge(self) -> None:
        """Wipe both in-memory cache and persistent file. Destructive — use
        only in tests or explicit administrative cleanup.
        """
        with self._lock:
            self._cache.clear()
            if self.path.exists():
                self.path.unlink()
