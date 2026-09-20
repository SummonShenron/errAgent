import hashlib

import pytest

from erragent.config import CloudConfig, ErrAgentConfig
from erragent.local import daemon


def _config() -> ErrAgentConfig:
    return ErrAgentConfig(
        cloud=CloudConfig(
            url="https://erragent.example",
            service="smoke",
            app_id=None,
            app_secret=None,
            ingest_secret="secret",
            timeout_seconds=5.0,
        ),
        local=None,
        local_only=False,
    )


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, config, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path.endswith("/approve"):
            return self.approve_response
        if method == "POST" and path.endswith("/ack"):
            return {"status": body["outcome"]}
        raise AssertionError(f"unexpected call: {method} {path}")


class _FakeLoop:
    def __init__(self):
        self.handler = None
        self.default_calls = []

    def set_exception_handler(self, handler):
        self.handler = handler

    def default_exception_handler(self, context):
        self.default_calls.append(context)


def test_benign_reset_filter_swallows_winerror_10054_connection_reset():
    loop = _FakeLoop()
    daemon._install_benign_reset_filter(loop)

    exception = ConnectionResetError()
    exception.winerror = 10054
    loop.handler(loop, {"message": "Exception in callback", "exception": exception})

    assert loop.default_calls == []


def test_benign_reset_filter_forwards_other_exceptions_to_default_handler():
    loop = _FakeLoop()
    daemon._install_benign_reset_filter(loop)

    exception = ValueError("something unrelated broke")
    context = {"message": "Exception in callback", "exception": exception}
    loop.handler(loop, context)

    assert loop.default_calls == [context]


def test_benign_reset_filter_forwards_connection_reset_with_different_winerror():
    # Only the specific WinError 10054 shape is benign noise from a --reload-killed connection;
    # any other ConnectionResetError should still surface normally.
    loop = _FakeLoop()
    daemon._install_benign_reset_filter(loop)

    exception = ConnectionResetError()
    exception.winerror = 10053  # a different error code, not the one we're filtering
    context = {"message": "Exception in callback", "exception": exception}
    loop.handler(loop, context)

    assert loop.default_calls == [context]


async def test_handle_error_event_reports_to_cloud_even_when_env_sets_local_url(tmp_path, monkeypatch, caplog):
    # Regression: the daemon loads the same .env as the app it's serving. That .env sets
    # ERRAGENT_LOCAL_URL (to tell the *app* to route through the daemon instead of the cloud
    # directly), which used to also make the daemon itself think it was in local-only mode and
    # skip cloud reporting entirely — even with valid ERRAGENT_URL/ERRAGENT_INGEST_SECRET set.
    monkeypatch.setenv("ERRAGENT_SERVICE", "svc")
    monkeypatch.setenv("ERRAGENT_URL", "https://erragent.example")
    monkeypatch.setenv("ERRAGENT_INGEST_SECRET", "secret")
    monkeypatch.setenv("ERRAGENT_LOCAL_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("ERRAGENT_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("ERRAGENT_APP_ID", raising=False)
    monkeypatch.delenv("ERRAGENT_APP_SECRET", raising=False)

    target = tmp_path / "app.py"
    target.write_text("print('broken')\n", encoding="utf-8")

    recorder = _Recorder()
    recorder.approve_response = {}
    monkeypatch.setattr(daemon, "_cloud_request", recorder)
    monkeypatch.setattr(
        daemon, "resolve_target_file", lambda root, message, context: ("app.py", target)
    )

    event = daemon.LogEvent(service="svc", level="error", message="boom")
    with caplog.at_level("ERROR"):
        await daemon._handle_error_event(tmp_path, event, poll_interval=0.01, poll_timeout=0.01)

    assert "No cloud credentials configured" not in caplog.text
    ingest_calls = [c for c in recorder.calls if c[1] == "/api/v1/webhooks/ingest"]
    assert len(ingest_calls) == 1


async def test_apply_proposal_writes_file_and_acks_success(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    original = "print('broken')\n"
    fixed = "print('fixed')\n"
    target.write_text(original, encoding="utf-8")

    recorder = _Recorder()
    recorder.approve_response = {
        "target_file_path": "app.py",
        "full_file_content": fixed,
        "full_file_content_sha256": hashlib.sha256(fixed.encode()).hexdigest(),
        "base_file_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "content_source": "sandbox_applied",
    }
    monkeypatch.setattr(daemon, "_cloud_request", recorder)

    await daemon._apply_proposal(_config(), tmp_path, "app.py", target, "proposal-1")

    assert target.read_text(encoding="utf-8") == fixed
    ack_calls = [c for c in recorder.calls if c[1].endswith("/ack")]
    assert len(ack_calls) == 1
    assert ack_calls[0][2]["outcome"] == "succeeded"


async def test_apply_proposal_refuses_when_file_changed_since_analysis(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    original = "print('broken')\n"
    target.write_text(original, encoding="utf-8")

    recorder = _Recorder()
    fixed = "print('fixed')\n"
    recorder.approve_response = {
        "target_file_path": "app.py",
        "full_file_content": fixed,
        "full_file_content_sha256": hashlib.sha256(fixed.encode()).hexdigest(),
        # base_file_sha256 does NOT match the current on-disk content — someone edited the
        # file locally between analysis and approval.
        "base_file_sha256": hashlib.sha256(b"something else entirely").hexdigest(),
        "content_source": "sandbox_applied",
    }
    monkeypatch.setattr(daemon, "_cloud_request", recorder)

    await daemon._apply_proposal(_config(), tmp_path, "app.py", target, "proposal-2")

    assert target.read_text(encoding="utf-8") == original  # untouched
    ack_calls = [c for c in recorder.calls if c[1].endswith("/ack")]
    assert ack_calls[0][2]["outcome"] == "failed"
    assert "changed since analysis" in ack_calls[0][2]["detail"]


async def test_apply_proposal_refuses_unverified_content_source(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    original = "print('broken')\n"
    target.write_text(original, encoding="utf-8")

    recorder = _Recorder()
    recorder.approve_response = {
        "target_file_path": "app.py",
        "full_file_content": "print('fixed')\n",
        "full_file_content_sha256": "irrelevant",
        "base_file_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "content_source": "llm_raw",  # not sandbox-verified
    }
    monkeypatch.setattr(daemon, "_cloud_request", recorder)

    await daemon._apply_proposal(_config(), tmp_path, "app.py", target, "proposal-3")

    assert target.read_text(encoding="utf-8") == original
    ack_calls = [c for c in recorder.calls if c[1].endswith("/ack")]
    assert ack_calls[0][2]["outcome"] == "failed"


async def test_apply_proposal_refuses_on_hash_mismatch(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    original = "print('broken')\n"
    target.write_text(original, encoding="utf-8")

    recorder = _Recorder()
    recorder.approve_response = {
        "target_file_path": "app.py",
        "full_file_content": "print('fixed')\n",
        "full_file_content_sha256": "not-the-real-hash",
        "base_file_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "content_source": "sandbox_applied",
    }
    monkeypatch.setattr(daemon, "_cloud_request", recorder)

    await daemon._apply_proposal(_config(), tmp_path, "app.py", target, "proposal-4")

    assert target.read_text(encoding="utf-8") == original
    ack_calls = [c for c in recorder.calls if c[1].endswith("/ack")]
    assert ack_calls[0][2]["outcome"] == "failed"
    assert "integrity check" in ack_calls[0][2]["detail"]


def test_ingest_log_endpoint_schedules_only_error_events(tmp_path):
    from fastapi.testclient import TestClient

    app = daemon.create_app(tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/v1/logs",
        json=[
            {"service": "svc", "level": "info", "message": "fine"},
            {"service": "svc", "level": "error", "message": "boom"},
        ],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["errorCount"] == 1


def test_health_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    app = daemon.create_app(tmp_path)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["root"] == str(tmp_path.resolve())


class _FakeCloud:
    """Minimal stateful fake of the cloud endpoints touched during a free-text investigation
    (submit -> service a pending local-tool request -> poll -> approve/decline), so the full
    flow can be exercised without a real backend."""

    def __init__(self, incident_id="inc-freetext-1"):
        self.incident_id = incident_id
        self.pending_tool_request = None
        self.tool_results = []
        self.proposal = None
        self.decline_calls = 0
        self._next_request_id = 0

    def queue_tool_request(self, tool_action, args):
        self._next_request_id += 1
        self.pending_tool_request = {"_id": f"req-{self._next_request_id}", "tool_action": tool_action, "args": args}

    async def __call__(self, config, method, path, body=None):
        if method == "POST" and path == "/api/v1/webhooks/ingest":
            return {"incident_id": self.incident_id}
        if method == "GET" and path == f"/api/v1/local-patch/{self.incident_id}/pending-tool-request":
            return {"request": self.pending_tool_request}
        if method == "POST" and path == f"/api/v1/local-patch/{self.incident_id}/tool-result":
            assert self.pending_tool_request is not None
            assert body["request_id"] == self.pending_tool_request["_id"]
            self.tool_results.append((body["request_id"], body["observation"]))
            self.pending_tool_request = None
            return {"status": "accepted"}
        if method == "GET" and path == f"/api/v1/local-patch/by-incident/{self.incident_id}":
            return {"proposal": self.proposal} if self.proposal else {}
        if method == "POST" and path.endswith("/decline"):
            self.decline_calls += 1
            return {}
        raise AssertionError(f"unexpected call: {method} {path}")


def _set_freetext_env(monkeypatch):
    monkeypatch.setenv("ERRAGENT_SERVICE", "svc")
    monkeypatch.setenv("ERRAGENT_URL", "https://erragent.example")
    monkeypatch.setenv("ERRAGENT_INGEST_SECRET", "secret")
    monkeypatch.delenv("ERRAGENT_LOCAL_URL", raising=False)
    monkeypatch.delenv("ERRAGENT_LOCAL_ONLY", raising=False)
    monkeypatch.delenv("ERRAGENT_APP_ID", raising=False)
    monkeypatch.delenv("ERRAGENT_APP_SECRET", raising=False)


async def test_service_pending_tool_request_services_read_local_file(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("value = None\n", encoding="utf-8")
    fake_cloud = _FakeCloud()
    fake_cloud.queue_tool_request("read_local_file", {"path": "app.py"})
    monkeypatch.setattr(daemon, "_cloud_request", fake_cloud)

    await daemon._service_pending_tool_request(_config(), tmp_path, fake_cloud.incident_id)

    assert fake_cloud.tool_results == [("req-1", "value = None\n")]
    assert fake_cloud.pending_tool_request is None


async def test_service_pending_tool_request_services_list_local_tree(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    fake_cloud = _FakeCloud()
    fake_cloud.queue_tool_request("list_local_tree", {})
    monkeypatch.setattr(daemon, "_cloud_request", fake_cloud)

    await daemon._service_pending_tool_request(_config(), tmp_path, fake_cloud.incident_id)

    assert fake_cloud.tool_results == [("req-1", "app.py")]


async def test_service_pending_tool_request_is_a_noop_when_nothing_pending(tmp_path, monkeypatch):
    fake_cloud = _FakeCloud()
    monkeypatch.setattr(daemon, "_cloud_request", fake_cloud)

    await daemon._service_pending_tool_request(_config(), tmp_path, fake_cloud.incident_id)

    assert fake_cloud.tool_results == []


async def test_handle_freetext_investigation_services_tool_request_before_declining(tmp_path, monkeypatch):
    # End-to-end shape of the bridge: a tool request queued before the proposal is ready must
    # be answered before _poll_for_proposal ever hands back that proposal.
    (tmp_path / "signup.py").write_text("redirect = None\n", encoding="utf-8")
    _set_freetext_env(monkeypatch)

    fake_cloud = _FakeCloud()
    fake_cloud.queue_tool_request("read_local_file", {"path": "signup.py"})
    fake_cloud.proposal = {
        "_id": "proposal-1",
        "status": "awaiting_approval",
        "action": {"target_file_path": "signup.py", "diff_preview": "diff", "pr_title": "Fix redirect"},
    }
    monkeypatch.setattr(daemon, "_cloud_request", fake_cloud)
    monkeypatch.setattr(daemon, "prompt_approval", lambda **kwargs: False)

    await daemon._handle_freetext_investigation(
        tmp_path, "signup doesn't redirect", poll_interval=0.01, poll_timeout=1.0
    )

    assert fake_cloud.tool_results == [("req-1", "redirect = None\n")]
    assert fake_cloud.decline_calls == 1


async def test_handle_freetext_investigation_refuses_target_file_escaping_root(tmp_path, monkeypatch):
    _set_freetext_env(monkeypatch)
    fake_cloud = _FakeCloud()
    fake_cloud.proposal = {
        "_id": "proposal-1",
        "status": "awaiting_approval",
        "action": {"target_file_path": "../../etc/passwd", "diff_preview": "diff", "pr_title": "t"},
    }
    monkeypatch.setattr(daemon, "_cloud_request", fake_cloud)
    approval_calls = []
    monkeypatch.setattr(daemon, "prompt_approval", lambda **kwargs: approval_calls.append(kwargs) or False)

    await daemon._handle_freetext_investigation(
        tmp_path, "something's broken", poll_interval=0.01, poll_timeout=1.0
    )

    # Must never even show a diff for a path that escapes the project root.
    assert approval_calls == []
    assert fake_cloud.decline_calls == 0
