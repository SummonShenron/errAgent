import io
import json
import urllib.error

import pytest

import erragent
from erragent import reader
from erragent.client import ErrAgentNotConfigured
from erragent.config import load_read_config


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


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ERRAGENT_URL", "https://erragent.example/")
    monkeypatch.setenv("ERRAGENT_APP_ID", "saapp")
    monkeypatch.setenv("ERRAGENT_READ_SECRET", "ear_super-secret")
    monkeypatch.delenv("ERRAGENT_LOCAL_URL", raising=False)
    monkeypatch.delenv("ERRAGENT_LOCAL_ONLY", raising=False)


def fake_urlopen(monkeypatch, body, code=200):
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return _FakeResponse(json.dumps(body).encode("utf-8"), code)

    monkeypatch.setattr(reader.urllib.request, "urlopen", urlopen)
    return seen


# ---- configuration -----------------------------------------------------------------------------------------------------

def test_read_config_needs_the_url_the_app_id_and_the_read_secret(monkeypatch):
    for name in ("ERRAGENT_URL", "ERRAGENT_APP_ID", "ERRAGENT_READ_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert load_read_config() is None
    monkeypatch.setenv("ERRAGENT_URL", "https://x")
    monkeypatch.setenv("ERRAGENT_APP_ID", "app")
    assert load_read_config() is None  # still no read secret
    monkeypatch.setenv("ERRAGENT_READ_SECRET", "s")
    assert load_read_config().read_secret == "s"


def test_the_ingest_secret_is_never_used_for_reads(monkeypatch):
    for name in ("ERRAGENT_READ_SECRET",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ERRAGENT_URL", "https://x")
    monkeypatch.setenv("ERRAGENT_APP_ID", "app")
    monkeypatch.setenv("ERRAGENT_APP_SECRET", "ingest-only")
    monkeypatch.setenv("ERRAGENT_INGEST_SECRET", "ingest-only")
    assert load_read_config() is None


def test_reads_still_work_in_local_dev_mode_where_reporting_is_switched_off(configured, monkeypatch):
    monkeypatch.setenv("ERRAGENT_LOCAL_URL", "http://127.0.0.1:8765")
    assert load_read_config() is not None
    seen = fake_urlopen(monkeypatch, {"incidents": []})
    assert asyncio_run(erragent.list_incidents()) == []
    assert seen[0].full_url.startswith("https://erragent.example/api/v1/app/incidents")


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


async def test_unconfigured_raises_not_configured(monkeypatch):
    for name in ("ERRAGENT_URL", "ERRAGENT_APP_ID", "ERRAGENT_READ_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ErrAgentNotConfigured):
        await reader.list_incidents()
    with pytest.raises(ErrAgentNotConfigured):
        await reader.latest_deploy()


# ---- requests ----------------------------------------------------------------------------------------------------------

async def test_list_incidents_sends_the_read_credential_and_the_filters(configured, monkeypatch):
    seen = fake_urlopen(monkeypatch, {"incidents": [{"id": "inc_1"}]})
    result = await reader.list_incidents(status="open", since="24h", limit=5)
    assert result == [{"id": "inc_1"}]
    request = seen[0]
    assert request.get_method() == "GET"
    assert request.get_header("X-app-id") == "saapp" and request.get_header("X-read-secret") == "ear_super-secret"
    assert "x-ingest-secret" not in {k.lower() for k in request.headers}
    for part in ("status=open", "since=24h", "limit=5", "environment=production"):
        assert part in request.full_url


async def test_the_page_size_is_clamped_on_the_client_too(configured, monkeypatch):
    seen = fake_urlopen(monkeypatch, {"incidents": []})
    await reader.list_incidents(limit=10_000)
    await reader.list_incidents(limit=0)
    assert "limit=50" in seen[0].full_url and "limit=1&" in seen[1].full_url + "&"


async def test_none_filters_are_left_out_of_the_query(configured, monkeypatch):
    seen = fake_urlopen(monkeypatch, {"incidents": []})
    await reader.list_incidents(environment=None)
    assert "status=" not in seen[0].full_url and "since=" not in seen[0].full_url and "environment=" not in seen[0].full_url


async def test_get_incident_encodes_the_id_so_it_cannot_change_the_path(configured, monkeypatch):
    seen = fake_urlopen(monkeypatch, {"incident": {"id": "x"}})
    assert await reader.get_incident("inc_1/../../admin?x=1") == {"id": "x"}
    assert seen[0].full_url == "https://erragent.example/api/v1/app/incidents/inc_1%2F..%2F..%2Fadmin%3Fx%3D1"
    with pytest.raises(ValueError):
        await reader.get_incident("   ")


async def test_latest_deploy_returns_the_payload(configured, monkeypatch):
    fake_urlopen(monkeypatch, {"provider": "render", "status": "not_configured", "reason": "x"})
    assert (await reader.latest_deploy())["status"] == "not_configured"


async def test_an_unexpected_response_shape_degrades_to_empty_not_an_exception(configured, monkeypatch):
    fake_urlopen(monkeypatch, ["not", "a", "dict"])
    assert await reader.list_incidents() == [] and await reader.get_incident("a") == {} and await reader.latest_deploy() == {}


# ---- failures -----------------------------------------------------------------------------------------------------------

def http_error(code, detail="nope"):
    return urllib.error.HTTPError("https://erragent.example", code, "err", {}, io.BytesIO(json.dumps({"detail": detail}).encode("utf-8")))


async def test_an_http_error_becomes_a_read_error_with_the_status_and_never_the_secret(configured, monkeypatch):
    def urlopen(request, timeout):
        raise http_error(401, "Invalid read credentials")

    monkeypatch.setattr(reader.urllib.request, "urlopen", urlopen)
    with pytest.raises(reader.ErrAgentReadError) as excinfo:
        await reader.list_incidents()
    assert excinfo.value.status_code == 401 and "Invalid read credentials" in str(excinfo.value)
    assert "ear_super-secret" not in str(excinfo.value)


async def test_an_unreachable_server_is_a_read_error_without_a_status(configured, monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.URLError("connection refused to https://erragent.example with ear_super-secret")

    monkeypatch.setattr(reader.urllib.request, "urlopen", urlopen)
    with pytest.raises(reader.ErrAgentReadError) as excinfo:
        await reader.get_incident("inc_1")
    assert excinfo.value.status_code is None and "ear_super-secret" not in str(excinfo.value)


async def test_a_non_json_body_is_a_read_error(configured, monkeypatch):
    monkeypatch.setattr(reader.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(b"<html>502</html>"))
    with pytest.raises(reader.ErrAgentReadError, match="not JSON"):
        await reader.list_incidents()


def test_the_reader_is_exported_from_the_package():
    for name in ("list_incidents", "get_incident", "latest_deploy", "list_logs", "ErrAgentReadError", "load_read_config"):
        assert name in erragent.__all__ and hasattr(erragent, name)


async def test_list_logs_sends_validated_filters_and_clamps_the_page(configured, monkeypatch):
    seen = fake_urlopen(monkeypatch, {"entries": [{"message": "hi"}], "matched": 1, "buffered": 4, "oldest_buffered": "2026-10-10T08:00:00Z"})
    result = await reader.list_logs(level="warn", since="6h", contains="time out", request_id="req-1", limit=9999)
    assert result["matched"] == 1 and result["entries"][0]["message"] == "hi"
    url = seen[0].full_url
    assert url.startswith("https://erragent.example/api/v1/app/logs?")
    assert "level=warn" in url and "since=6h" in url and "contains=time+out" in url and "request_id=req-1" in url and "limit=100" in url
    assert seen[0].get_header("X-read-secret") == "ear_super-secret"


async def test_list_logs_leaves_out_filters_that_were_not_given(configured, monkeypatch):
    seen = fake_urlopen(monkeypatch, {"entries": []})
    await reader.list_logs()
    assert seen[0].full_url.endswith("/api/v1/app/logs?limit=50")


async def test_list_logs_failures_never_carry_the_secret(configured, monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, io.BytesIO(b'{"detail": "Invalid read credentials"}'))

    monkeypatch.setattr(reader.urllib.request, "urlopen", refuse)
    with pytest.raises(reader.ErrAgentReadError) as excinfo:
        await reader.list_logs()
    assert excinfo.value.status_code == 401 and "ear_super-secret" not in str(excinfo.value)
