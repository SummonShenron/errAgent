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
