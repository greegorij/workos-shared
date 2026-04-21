"""Tests for workos_shared.openrouter (E2 S6 stub, s87)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from workos_shared.openrouter import (
    OpenRouterClient,
    OpenRouterError,
    _resolve_api_key,
)


# --- Key resolution ---------------------------------------------------------


def test_resolve_api_key_explicit():
    assert _resolve_api_key("explicit-xyz") == "explicit-xyz"


def test_resolve_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-abc")
    assert _resolve_api_key(None) == "env-abc"


def test_resolve_api_key_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env_file = tmp_path / ".claude" / ".openrouter.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("OPENROUTER_API_KEY=file-def\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_api_key(None) == "file-def"


def test_resolve_api_key_file_plain_content(tmp_path, monkeypatch):
    """File may contain just the key (no KEY=...) — fallback handling."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    env_file = tmp_path / ".claude" / ".openrouter.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("plain-ghi")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_api_key(None) == "plain-ghi"


def test_resolve_api_key_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ValueError, match="API key not found"):
        _resolve_api_key(None)


# --- Client construction ----------------------------------------------------


def test_client_init_with_explicit_key():
    client = OpenRouterClient(api_key="test-key")
    assert client.api_key == "test-key"
    assert client.base_url == "https://openrouter.ai/api/v1"
    assert client.timeout == 60


def test_client_init_custom_base_url_and_timeout():
    client = OpenRouterClient(
        api_key="k",
        base_url="https://custom.example.com/v1/",
        timeout=30,
        app_name="my-app",
        app_url="https://example.com",
    )
    assert client.base_url == "https://custom.example.com/v1"  # trailing / stripped
    assert client.timeout == 30
    assert client.app_name == "my-app"
    assert client.app_url == "https://example.com"


def test_client_headers_include_auth_and_optional():
    client = OpenRouterClient(api_key="tok", app_name="A", app_url="https://u.example")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Title"] == "A"
    assert headers["HTTP-Referer"] == "https://u.example"


def test_client_headers_without_optional():
    client = OpenRouterClient(api_key="tok")
    headers = client._headers()
    assert "X-Title" not in headers
    assert "HTTP-Referer" not in headers


# --- Chat + list_models (mock urllib) ---------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_chat_builds_correct_payload():
    client = OpenRouterClient(api_key="k")
    fake_response = _FakeResponse({"choices": [{"message": {"content": "Hello"}}]})

    with mock.patch("urllib.request.urlopen", return_value=fake_response) as m_open:
        response = client.chat(
            model="google/gemini-2.5-flash",
            messages=[{"role": "user", "content": "Hi"}],
            temperature=0.5,
        )

    assert response["choices"][0]["message"]["content"] == "Hello"
    m_open.assert_called_once()
    request = m_open.call_args[0][0]
    assert request.method == "POST"
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "google/gemini-2.5-flash"
    assert body["temperature"] == 0.5
    assert body["messages"][0]["content"] == "Hi"


def test_list_models_caches_result():
    client = OpenRouterClient(api_key="k")
    fake_response = _FakeResponse({"data": [{"id": "a/b"}, {"id": "c/d"}]})

    with mock.patch("urllib.request.urlopen", return_value=fake_response) as m_open:
        models1 = client.list_models()
        models2 = client.list_models()

    assert models1 == models2 == [{"id": "a/b"}, {"id": "c/d"}]
    assert m_open.call_count == 1  # second call served from cache


def test_list_models_force_refresh():
    client = OpenRouterClient(api_key="k")
    fake_response = _FakeResponse({"data": [{"id": "x/y"}]})

    with mock.patch("urllib.request.urlopen", return_value=fake_response) as m_open:
        client.list_models()
        client.list_models(force_refresh=True)

    assert m_open.call_count == 2


def test_chat_http_error_raises_openrouter_error():
    import urllib.error

    client = OpenRouterClient(api_key="k")
    http_error = urllib.error.HTTPError(
        url="https://openrouter.ai/api/v1/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    http_error.read = lambda: b'{"error": "rate limit"}'

    with mock.patch("urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(OpenRouterError) as exc_info:
            client.chat(model="m", messages=[])

    assert exc_info.value.status_code == 429
    assert "rate limit" in exc_info.value.body
