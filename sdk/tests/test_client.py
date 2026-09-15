import json

import pytest

from erragent import client


class _FakeResponse:
    def __init__(self, body: bytes, code: int = 200):
        self._body = body
        self._code = code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self._code

    def read(self):
        return self._body


async def test_report_incident_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ERRAGENT_URL", raising=False)
    monkeypatch.delenv("ERRAGENT_APP_ID", raising=False)
    monkeypatch.delenv("ERRAGENT_APP_SECRET", raising=False)
    monkeypatch.delenv("ERRAGENT_INGEST_SECRET", raising=False)

    with pytest.raises(client.ErrAgentNotConfigured):
        await client.report_incident(error_message="boom")


async def test_report_incident_posts_expected_payload_and_headers(monkeypatch):
    monkeypatch.setenv("ERRAGENT_URL", "https://erragent.example")
    monkeypatch.setenv("ERRAGENT_SERVICE", "my-service")
    monkeypatch.setenv("ERRAGENT_APP_ID", "app-123")
    monkeypatch.setenv("ERRAGENT_APP_SECRET", "shh")
    monkeypatch.delenv("ERRAGENT_INGEST_SECRET", raising=False)

    captured_requests = []

    def fake_urlopen(request, timeout):
        captured_requests.append(request)
        return _FakeResponse(b'{"status":"accepted"}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    result = await client.report_incident(
        error_message="Document download failed",
        stack_trace="Traceback...",
        repository="org/repo",
        metadata={"route": "/download"},
    )

    assert result["status_code"] == 200
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.full_url == "https://erragent.example/api/v1/webhooks/ingest"
    assert request.get_header("X-app-id") == "app-123"
    assert request.get_header("X-ingest-secret") == "shh"

    payload = json.loads(request.data.decode("utf-8"))
    assert payload["service_name"] == "my-service"
    assert payload["error_message"] == "Document download failed"
    assert payload["repository"] == "org/repo"
    assert payload["metadata"] == {"route": "/download"}


async def test_report_incident_nowait_does_not_raise(monkeypatch):
    monkeypatch.delenv("ERRAGENT_URL", raising=False)
    # Unconfigured: the background task will hit ErrAgentNotConfigured internally and
    # swallow it via _run_nowait's error handling — the call itself must never raise.
    client.report_incident_nowait(error_message="boom")


async def test_report_client_error_posts_expected_payload(monkeypatch):
    monkeypatch.setenv("ERRAGENT_URL", "https://erragent.example")
    monkeypatch.setenv("ERRAGENT_SERVICE", "bty")
    monkeypatch.setenv("ERRAGENT_APP_ID", "bty")
    monkeypatch.setenv("ERRAGENT_APP_SECRET", "shh")
    monkeypatch.delenv("ERRAGENT_INGEST_SECRET", raising=False)

    captured_requests = []

    def fake_urlopen(request, timeout):
        captured_requests.append(request)
        return _FakeResponse(b'{"status":"accepted"}')

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    result = await client.report_client_error(
        message="Something broke",
        route="/checkout",
        stack="Error: ...",
        metadata={"source": "frontend"},
    )

    assert result["status_code"] == 200
    request = captured_requests[0]
    assert request.full_url == "https://erragent.example/api/v1/client-errors"
    assert request.get_header("X-app-id") == "bty"

    payload = json.loads(request.data.decode("utf-8"))
    assert payload["service"] == "bty"
    assert payload["route"] == "/checkout"
    assert payload["message"] == "Something broke"


async def test_report_client_error_nowait_does_not_raise(monkeypatch):
    monkeypatch.delenv("ERRAGENT_URL", raising=False)
    client.report_client_error_nowait(message="boom")
