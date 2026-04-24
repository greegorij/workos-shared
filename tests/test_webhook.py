"""Tests for workos_shared.webhook.

Covers:
    1. verify_hmac_signature accepts valid ``sha256=<hex>``.
    2. verify_hmac_signature accepts bare hex digest (no scheme prefix).
    3. verify_hmac_signature is case-insensitive on algo name.
    4. verify_hmac_signature rejects wrong digest (SignatureMismatch).
    5. verify_hmac_signature rejects empty signature / empty secret / unsupported algo.
    6. verify_hmac_signature honours algo_hint override.
    7. PersistentDedup.add returns True first time, False on repeat, persists.
    8. PersistentDedup loads existing file on init (survives restart).
    9. PersistentDedup.discard removes from memory without touching file.
    10. PersistentDedup.purge wipes both memory and file.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from workos_shared.webhook import (
    PersistentDedup,
    SignatureMismatch,
    verify_hmac_signature,
)

SECRET = "test-secret"
BODY = b'{"event":"Transcription completed","meeting_id":"abc123"}'
VALID_HEX = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()


# --- HMAC verification ------------------------------------------------------


def test_hmac_accepts_sha256_scheme_prefix():
    # Must not raise.
    verify_hmac_signature(body=BODY, signature=f"sha256={VALID_HEX}", secret=SECRET)


def test_hmac_accepts_bare_hex_digest():
    # No "sha256=" prefix at all.
    verify_hmac_signature(body=BODY, signature=VALID_HEX, secret=SECRET)


def test_hmac_scheme_is_case_insensitive():
    verify_hmac_signature(body=BODY, signature=f"SHA256={VALID_HEX}", secret=SECRET)


def test_hmac_rejects_wrong_digest():
    bad = "0" * 64
    with pytest.raises(SignatureMismatch):
        verify_hmac_signature(body=BODY, signature=f"sha256={bad}", secret=SECRET)


def test_hmac_rejects_empty_inputs():
    with pytest.raises(SignatureMismatch):
        verify_hmac_signature(body=BODY, signature="", secret=SECRET)
    with pytest.raises(SignatureMismatch):
        verify_hmac_signature(body=BODY, signature="sha256=", secret=SECRET)
    with pytest.raises(SignatureMismatch):
        verify_hmac_signature(body=BODY, signature=f"sha256={VALID_HEX}", secret="")
    with pytest.raises(SignatureMismatch):
        verify_hmac_signature(body=BODY, signature=f"md5={VALID_HEX}", secret=SECRET)


def test_hmac_algo_hint_override():
    # Header says sha256 but we force sha1 — digests won't match.
    with pytest.raises(SignatureMismatch):
        verify_hmac_signature(
            body=BODY,
            signature=f"sha256={VALID_HEX}",
            secret=SECRET,
            algo_hint="sha1",
        )
    # Valid sha1 + hint passes.
    sha1_hex = hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()
    verify_hmac_signature(
        body=BODY,
        signature=f"sha256={sha1_hex}",  # wrong scheme label
        secret=SECRET,
        algo_hint="sha1",  # but hint wins
    )


# --- PersistentDedup --------------------------------------------------------


def test_dedup_add_persists_and_dedupes(tmp_path):
    dedup = PersistentDedup(tmp_path / "ids.txt")
    assert dedup.add("meeting-1") is True
    assert dedup.add("meeting-1") is False  # dupe
    assert "meeting-1" in dedup
    assert dedup.contains("meeting-1") is True
    # File content has the ID.
    assert (tmp_path / "ids.txt").read_text().strip() == "meeting-1"


def test_dedup_loads_existing_file_on_init(tmp_path):
    path = tmp_path / "ids.txt"
    path.write_text("meeting-a\nmeeting-b\n")
    dedup = PersistentDedup(path)
    assert "meeting-a" in dedup
    assert "meeting-b" in dedup
    assert len(dedup) == 2
    # Re-adding existing IDs returns False.
    assert dedup.add("meeting-a") is False


def test_dedup_discard_memory_only(tmp_path):
    path = tmp_path / "ids.txt"
    dedup = PersistentDedup(path)
    dedup.add("meeting-x")
    dedup.discard("meeting-x")
    assert "meeting-x" not in dedup
    # File still has it (discard does NOT rewrite disk).
    assert "meeting-x" in path.read_text()
    # Reloading reinstates from disk.
    dedup2 = PersistentDedup(path)
    assert "meeting-x" in dedup2


def test_dedup_purge_wipes_memory_and_file(tmp_path):
    path = tmp_path / "ids.txt"
    dedup = PersistentDedup(path)
    dedup.add("meeting-p")
    assert path.exists()
    dedup.purge()
    assert len(dedup) == 0
    assert not path.exists()


def test_dedup_rejects_empty_key(tmp_path):
    dedup = PersistentDedup(tmp_path / "ids.txt")
    with pytest.raises(ValueError):
        dedup.add("")
