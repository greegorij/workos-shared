"""Tests for workos_shared.anthropic_client.

Covers:
    1. detect_long_prompt — threshold math.
    2. parse_json_response — raw JSON.
    3. parse_json_response — markdown-fenced JSON with ```json.
    4. parse_json_response — malformed input returns None.
    5. call_claude routes to Batch path on success.
    6. call_claude falls back to sync when batch times out.
    7. call_claude takes streaming path for long prompts (skips Batch).
    8. call_claude raises AnthropicClientError when sync fallback fails too.
    9. call_claude_async routes to Batch (async sleep instead of time.sleep).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import asyncio

import pytest

from workos_shared import anthropic_client as ac


# --- Helpers: a minimal fake Anthropic client ------------------------------


def _make_fake_client(
    *,
    batch_outcome: str = "succeeded",
    batch_text: str = '{"ok": true}',
    sync_text: str = '{"ok": "sync"}',
    stream_text: str = '{"ok": "stream"}',
    batch_raises: Exception | None = None,
    sync_raises: Exception | None = None,
    stream_raises: Exception | None = None,
    batch_processing_statuses: list[str] | None = None,
):
    """Return a mock anthropic.Anthropic()-like client.

    ``batch_processing_statuses`` controls what ``retrieve()`` returns on
    successive polls — default is a single "ended" status so one poll resolves.
    """
    client = MagicMock(name="AnthropicClient")
    statuses = list(batch_processing_statuses or ["ended"])

    def _batches_create(**kwargs):
        if batch_raises:
            raise batch_raises
        return SimpleNamespace(id="batch-fake-123")

    def _batches_retrieve(batch_id):
        if statuses:
            status = statuses.pop(0)
        else:
            status = "ended"
        return SimpleNamespace(processing_status=status)

    def _batches_results(batch_id):
        if batch_outcome == "succeeded":
            msg = SimpleNamespace(content=[SimpleNamespace(text=batch_text)])
            return iter(
                [
                    SimpleNamespace(
                        result=SimpleNamespace(type="succeeded", message=msg)
                    )
                ]
            )
        return iter(
            [SimpleNamespace(result=SimpleNamespace(type="errored", error="nope"))]
        )

    client.messages = MagicMock()
    client.messages.batches = MagicMock()
    client.messages.batches.create = MagicMock(side_effect=_batches_create)
    client.messages.batches.retrieve = MagicMock(side_effect=_batches_retrieve)
    client.messages.batches.results = MagicMock(side_effect=_batches_results)

    def _messages_create(**kwargs):
        if sync_raises:
            raise sync_raises
        return SimpleNamespace(
            content=[SimpleNamespace(text=sync_text)]
        )

    client.messages.create = MagicMock(side_effect=_messages_create)

    # Streaming stub: context manager yielding text chunks.
    class _StreamCtx:
        def __init__(self, text):
            self.text_stream = iter([text])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _messages_stream(**kwargs):
        if stream_raises:
            raise stream_raises
        return _StreamCtx(stream_text)

    client.messages.stream = MagicMock(side_effect=_messages_stream)

    return client


# --- Tests ------------------------------------------------------------------


def test_detect_long_prompt_threshold():
    assert ac.detect_long_prompt("x" * 100, threshold=50) is True
    assert ac.detect_long_prompt("x" * 40, threshold=50) is False
    # Multiple parts sum.
    assert ac.detect_long_prompt("x" * 30, "y" * 30, threshold=50) is True


def test_parse_json_response_plain():
    assert ac.parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_json_response_markdown_fenced():
    wrapped = '```json\n{"meeting_note": "ok"}\n```'
    assert ac.parse_json_response(wrapped) == {"meeting_note": "ok"}
    # Bare ``` without language tag.
    wrapped2 = '```\n{"x": 2}\n```'
    assert ac.parse_json_response(wrapped2) == {"x": 2}


def test_parse_json_response_malformed_returns_none(caplog):
    assert ac.parse_json_response("not json {{{") is None
    # Array isn't a dict — still returns None.
    assert ac.parse_json_response('["a", "b"]') is None


def test_call_claude_batch_path_success():
    client = _make_fake_client(batch_text='{"path": "batch"}')
    result = ac.call_claude(
        api_key="fake",
        system="sys",
        user_message="msg",
        custom_id="test-1",
        batch_poll_interval_s=0,
        _client=client,
        _sleep=lambda s: None,  # no waiting in tests
    )
    assert result.path == "batch"
    assert result.model == ac.DEFAULT_MODEL
    assert result.text == '{"path": "batch"}'
    # Streaming and plain sync must NOT have been called on batch success.
    client.messages.stream.assert_not_called()
    client.messages.create.assert_not_called()


def test_call_claude_batch_timeout_falls_back_to_sync():
    # All polls return "in_progress" → batch times out.
    client = _make_fake_client(
        batch_processing_statuses=["in_progress", "in_progress"],
        sync_text='{"path": "sync-fallback"}',
    )
    result = ac.call_claude(
        api_key="fake",
        system="sys",
        user_message="msg",
        custom_id="test-2",
        batch_poll_interval_s=0,
        batch_max_polls=2,  # short ceiling
        _client=client,
        _sleep=lambda s: None,
    )
    assert result.path == "sync"
    assert result.text == '{"path": "sync-fallback"}'
    client.messages.create.assert_called_once()


def test_call_claude_long_prompt_takes_stream_path():
    client = _make_fake_client(stream_text='{"path": "stream"}')
    # Threshold of 10 chars so any reasonable prompt triggers long-context.
    result = ac.call_claude(
        api_key="fake",
        system="xxxxxxx",
        user_message="yyyyyyy",
        custom_id="test-3",
        long_prompt_threshold=10,
        batch_poll_interval_s=0,
        _client=client,
        _sleep=lambda s: None,
    )
    assert result.path == "stream"
    assert result.model == ac.LONG_CONTEXT_MODEL
    # Batch must be skipped for long prompts.
    client.messages.batches.create.assert_not_called()
    # Streaming was used (not plain sync create).
    client.messages.stream.assert_called_once()
    client.messages.create.assert_not_called()


def test_call_claude_raises_when_both_batch_and_sync_fail():
    # Batch raises → falls back to sync. Sync also raises → AnthropicClientError.
    client = _make_fake_client(
        batch_raises=RuntimeError("batch boom"),
        sync_raises=RuntimeError("sync boom"),
    )
    with pytest.raises(ac.AnthropicClientError) as exc_info:
        ac.call_claude(
            api_key="fake",
            system="sys",
            user_message="msg",
            custom_id="test-4",
            batch_poll_interval_s=0,
            _client=client,
            _sleep=lambda s: None,
        )
    assert "sync fallback failed" in str(exc_info.value)


def test_call_claude_async_batch_path_success():
    client = _make_fake_client(batch_text='{"path": "async-batch"}')

    async def _run():
        return await ac.call_claude_async(
            api_key="fake",
            system="sys",
            user_message="msg",
            custom_id="test-5",
            batch_poll_interval_s=0,
            _client=client,
        )

    result = asyncio.run(_run())
    assert result.path == "batch"
    assert result.text == '{"path": "async-batch"}'


def test_call_claude_batch_disabled_goes_straight_to_sync():
    client = _make_fake_client(sync_text='{"skipped": "batch"}')
    result = ac.call_claude(
        api_key="fake",
        system="sys",
        user_message="msg",
        custom_id="test-6",
        batch_enabled=False,
        batch_poll_interval_s=0,
        _client=client,
        _sleep=lambda s: None,
    )
    assert result.path == "sync"
    client.messages.batches.create.assert_not_called()
    client.messages.create.assert_called_once()
