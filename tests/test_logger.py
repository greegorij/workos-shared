"""Tests for workos_shared.logger.

Covers 10 scenarios:
    1. Structured JSON output is parseable and has required fields.
    2. Plain text mode produces non-JSON output.
    3. Level filtering drops messages below the threshold.
    4. `extra={"context": {...}}` is merged under ``context``.
    5. Stray extras are also collected under ``context``.
    6. File rotation handler is attached and writes to LOG_DIR.
    7. Repeated ``get_logger`` calls don't double-attach handlers.
    8. Exception info is serialized.
    9. WebhookHandler posts ERROR+ only (INFO is not dispatched).
    10. Webhook failures don't break logging (network error swallowed).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from workos_shared.logger import (
    JsonFormatter,
    WebhookHandler,
    _configured,
    get_logger,
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    """Isolate each test — fresh LOG_DIR, clean logger registry."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("JARVIS_SERVICE", raising=False)
    _configured.clear()
    # Purge any leftover handlers from previous tests.
    for name in list(logging.root.manager.loggerDict.keys()):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
    yield
    _configured.clear()


def _read_log_file(log_dir: Path, service_name: str) -> str:
    path = log_dir / f"{service_name}.log"
    # Close handlers so contents are flushed.
    for lg in logging.root.manager.loggerDict.values():
        if isinstance(lg, logging.Logger):
            for h in lg.handlers:
                try:
                    h.flush()
                except Exception:
                    pass
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_structured_json_output(tmp_path, capsys):
    logger = get_logger("t.structured", service_name="svc-a", structured=True)
    logger.info("hello", extra={"context": {"k": "v"}})
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["service"] == "svc-a"
    assert payload["logger"] == "t.structured"
    assert payload["context"] == {"k": "v"}
    assert "timestamp" in payload


def test_plain_text_mode(tmp_path, capsys):
    logger = get_logger("t.plain", service_name="svc-b", structured=False)
    logger.info("no json here")
    out = capsys.readouterr().out.strip().splitlines()[-1]
    # Plain text formatter should not produce JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert "no json here" in out
    assert "[INFO]" in out


def test_level_filtering(tmp_path, capsys):
    logger = get_logger("t.level", service_name="svc-c", level="WARNING")
    logger.info("dropped")
    logger.warning("kept")
    lines = [l for l in capsys.readouterr().out.strip().splitlines() if l]
    assert not any("dropped" in l for l in lines)
    assert any("kept" in l for l in lines)


def test_explicit_context_dict(tmp_path, capsys):
    logger = get_logger("t.ctx", service_name="svc-d")
    logger.info("go", extra={"context": {"request_id": "abc", "user": "gg"}})
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["context"]["request_id"] == "abc"
    assert payload["context"]["user"] == "gg"


def test_stray_extras_collected_as_context(tmp_path, capsys):
    logger = get_logger("t.stray", service_name="svc-e")
    logger.info("go", extra={"chunks": 42, "vault": "Gregor"})
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["context"]["chunks"] == 42
    assert payload["context"]["vault"] == "Gregor"


def test_file_handler_writes_to_log_dir(tmp_path, capsys):
    logger = get_logger("t.file", service_name="svc-f")
    logger.info("written to disk")
    # Ensure file flush.
    for h in logger.handlers:
        h.flush()
    contents = _read_log_file(tmp_path, "svc-f")
    assert "written to disk" in contents
    # Should be valid JSON per line.
    last_line = contents.strip().splitlines()[-1]
    payload = json.loads(last_line)
    assert payload["message"] == "written to disk"


def test_idempotent_configuration(tmp_path, capsys):
    a = get_logger("t.same", service_name="svc-g")
    b = get_logger("t.same", service_name="svc-g")
    assert a is b
    # Handlers should not have doubled.
    handler_count_1 = len(a.handlers)
    _ = get_logger("t.same", service_name="svc-g")
    handler_count_2 = len(a.handlers)
    assert handler_count_1 == handler_count_2


def test_exception_info_serialized(tmp_path, capsys):
    logger = get_logger("t.exc", service_name="svc-h")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("caught")
    out_lines = [l for l in capsys.readouterr().out.strip().splitlines() if l]
    payload = json.loads(out_lines[-1])
    assert payload["message"] == "caught"
    assert "exception" in payload
    assert "ValueError" in payload["exception"]
    assert "boom" in payload["exception"]


class _CollectingWebhook(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        try:
            _CollectingWebhook.received.append(json.loads(body))
        except json.JSONDecodeError:
            _CollectingWebhook.received.append({"raw": body})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args, **kwargs):  # Silence noisy default logging.
        pass


def test_webhook_handler_only_fires_on_error(tmp_path):
    _CollectingWebhook.received = []
    server = HTTPServer(("127.0.0.1", 0), _CollectingWebhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/hook"
        logger = get_logger(
            "t.webhook",
            service_name="svc-i",
            webhook_url=url,
        )
        logger.info("skip me")
        logger.error("alert!")
        # WebhookHandler uses a worker thread — give it a beat.
        time.sleep(0.3)
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert len(_CollectingWebhook.received) == 1
    assert _CollectingWebhook.received[0]["level"] == "ERROR"
    assert _CollectingWebhook.received[0]["message"] == "alert!"


def test_webhook_network_failure_does_not_break_logging(tmp_path, capsys):
    # Point at a closed port — urlopen should fail silently.
    url = "http://127.0.0.1:1/hook"
    logger = get_logger(
        "t.webhook-fail",
        service_name="svc-j",
        webhook_url=url,
    )
    # Must not raise.
    logger.error("network down somewhere")
    time.sleep(0.2)
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["level"] == "ERROR"
    assert payload["message"] == "network down somewhere"


def test_env_overrides_level(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    logger = get_logger("t.env", service_name="svc-k", level="DEBUG")
    logger.info("not emitted")
    logger.error("emitted")
    out_lines = [l for l in capsys.readouterr().out.strip().splitlines() if l]
    assert all("not emitted" not in l for l in out_lines)
    assert any("emitted" in l for l in out_lines)


def test_json_formatter_directly():
    formatter = JsonFormatter(service_name="unit")
    record = logging.LogRecord(
        name="unit.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="direct test",
        args=(),
        exc_info=None,
    )
    record.context = {"x": 1}
    rendered = formatter.format(record)
    payload = json.loads(rendered)
    assert payload["message"] == "direct test"
    assert payload["context"]["x"] == 1
