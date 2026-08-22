import asyncio
import logging

from fastapi.testclient import TestClient

import backend.app.app as app_module
from backend.app.logging.logger import NOISY_LOGGERS
from backend.services.log_broker import InternalLogHandler, LogBroker, LogEventInput
from backend.services.log_broker import log_broker


def test_erragent_self_monitor_records_and_deduplicates(monkeypatch):
    from starlette.requests import Request
    import backend.app.app as app_module

    class Collection:
        def __init__(self):
            self.documents = []

        def find_one(self, query, **_kwargs):
            for document in reversed(self.documents):
                if all(document.get(key) == value for key, value in query.items() if not isinstance(value, dict)):
                    cutoff = query.get("created_at", {}).get("$gte")
                    if cutoff is None or document.get("created_at") >= cutoff:
                        return document
            return None

        def insert_one(self, document):
            self.documents.append(document)

    class DB:
        def __init__(self):
            self.collections = {"incidents": Collection(), "audit_logs": Collection()}

        def __getitem__(self, name):
            return self.collections[name]

    db = DB()
    monkeypatch.setattr(app_module, "get_db", lambda: db)
    request = Request({"type": "http", "method": "GET", "path": "/api/test", "headers": [], "query_string": b"", "server": ("test", 80)})

    first = app_module._record_erragent_exception(request, RuntimeError("boom"))
    second = app_module._record_erragent_exception(request, RuntimeError("boom"))

    assert first == second
    assert len(db["incidents"].documents) == 1
    assert db["incidents"].documents[0]["service_name"] == "erragent"
    assert db["incidents"].documents[0]["metadata"]["source"] == "erragent_self_monitor"


def test_patchy_operational_failures_are_reported_but_user_errors_are_not(monkeypatch):
    import backend.app.app as app_module

    calls = []
    monkeypatch.setattr(app_module, "_record_erragent_exception", lambda *args, **kwargs: calls.append((args, kwargs)))

    app_module._record_patchy_failure(object(), RuntimeError("GitHub API timed out"))
    app_module._record_patchy_failure(object(), ValueError("Usage: test plan <incident-id>"))

    assert len(calls) == 1
    assert calls[0][1]["source"] == "patchy_self_monitor"
    assert calls[0][1]["message_prefix"] == "Patchy operational failure"


def test_client_error_ingest_redacts_sensitive_fields(monkeypatch):
    import asyncio
    import backend.app.app as app_module
    from backend.app.app import ClientErrorRequest

    captured = []
    class FakeDB:
        pass

    monkeypatch.setattr(app_module, "get_db", lambda: FakeDB())
    monkeypatch.setattr(
        app_module,
        "authenticate_ingest_client",
        lambda *_args: {"actor": "MACHINE_INGEST:bty", "app_id": "bty", "default_repo": "owner/bty"},
    )
    monkeypatch.setattr(
        app_module,
        "ingest_machine_payload",
        lambda _db, _tasks, payload, _actor, **_kwargs: captured.append(payload) or "client_incident_1",
    )

    response = asyncio.run(app_module.ingest_client_error(
        ClientErrorRequest(
            service="btyapp",
            message="Request failed password=secret-value",
            metadata={"token": "jwt-value", "route": "/booking"},
        ),
        object(),
        "secret",
        "bty",
    ))

    assert response == {"status": "accepted", "incident_id": "client_incident_1"}
    assert captured[0]["error_message"] == "Request failed password=[REDACTED]"
    assert captured[0]["metadata"]["token"] == "[REDACTED]"
    assert captured[0]["metadata"]["source"] == "frontend"


def test_log_broker_retains_bounded_history_per_service():
    async def scenario():
        broker = LogBroker(max_entries_per_service=2)
        await broker.publish(LogEventInput(service="SAAPP", level="info", message="first"))
        await broker.publish(LogEventInput(service="SAAPP", level="warn", message="second"))
        await broker.publish(LogEventInput(service="SAAPP", level="error", message="third"))
        await broker.publish(LogEventInput(service="BTY", level="info", message="other service"))

        _, saapp_history = await broker.subscribe("SAAPP")
        _, bty_history = await broker.subscribe("BTY")

        assert [entry["message"] for entry in saapp_history] == ["second", "third"]
        assert [entry["message"] for entry in bty_history] == ["other service"]

    asyncio.run(scenario())


def test_log_broker_filters_history_and_live_entries():
    async def scenario():
        broker = LogBroker()
        await broker.publish(LogEventInput(service="SAAPP", level="info", message="ready"))
        await broker.publish(LogEventInput(service="SAAPP", level="error", message="failed"))

        queue, history = await broker.subscribe("SAAPP", level="error")
        assert [entry["message"] for entry in history] == ["failed"]

        await broker.publish(LogEventInput(service="SAAPP", level="warn", message="slow"))
        await broker.publish(LogEventInput(service="BTY", level="error", message="wrong service"))
        await broker.publish(LogEventInput(service="SAAPP", level="error", message="failed again"))

        assert (await asyncio.wait_for(queue.get(), timeout=0.1))["message"] == "failed again"
        assert queue.empty()

    asyncio.run(scenario())


def test_log_broker_normalizes_epoch_milliseconds():
    async def scenario():
        broker = LogBroker()
        entry = await broker.publish(
            LogEventInput(
                service="BTY",
                level="warn",
                message="slow response",
                timestamp=1723759200000,
            )
        )
        assert entry["timestamp"] == "2024-08-15T22:00:00Z"

    asyncio.run(scenario())


def test_internal_log_handler_excludes_configured_noisy_loggers():
    async def scenario():
        broker = LogBroker()
        handler = InternalLogHandler(broker, asyncio.get_running_loop())
        handler.emit(
            logging.LogRecord(
                "ErrAgent Logger",
                logging.WARNING,
                __file__,
                1,
                "Useful warning",
                (),
                None,
            )
        )
        for logger_name in NOISY_LOGGERS:
            handler.emit(
                logging.LogRecord(
                    logger_name,
                    logging.CRITICAL,
                    __file__,
                    1,
                    "Noise",
                    (),
                    None,
                )
            )

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        _, history = await broker.subscribe("errAgent")

        assert [entry["message"] for entry in history] == ["Useful warning"]
        assert history[0]["level"] == "warn"

    asyncio.run(scenario())


def test_log_ingestion_streams_to_authenticated_websocket(monkeypatch):
    asyncio.run(log_broker.clear())
    monkeypatch.setattr(app_module, "get_db", lambda: object())
    monkeypatch.setattr(
        app_module,
        "authenticate_ingest_client",
        lambda _db, _secret, app_id: {
            "actor": "MACHINE_INGEST:saapp",
            "app_id": app_id,
            "default_repo": None,
        },
    )
    monkeypatch.setattr(app_module, "decode_access_token", lambda _token: {"sub": "operator"})

    client = TestClient(app_module.app)
    with client.websocket_connect("/api/v1/live-logs?service=SAAPP&level=error") as websocket:
        websocket.send_json({"type": "auth", "token": "test-token"})
        assert websocket.receive_json() == {"type": "history", "entries": []}

        response = client.post(
            "/api/v1/logs",
            headers={"x-ingest-secret": "test-secret", "x-app-id": "saapp"},
            json={
                "service": "SAAPP",
                "level": "error",
                "message": "Dashboard failed",
                "context": {"route": "/dashboard"},
            },
        )

        assert response.status_code == 202
        message = websocket.receive_json()
        assert message["type"] == "log"
        assert message["entry"]["message"] == "Dashboard failed"
        assert message["entry"]["source_app_id"] == "saapp"


def test_error_log_creates_incident_but_info_and_warning_do_not(monkeypatch):
    captured_incidents = []

    class FakeDB:
        pass

    monkeypatch.setattr(app_module, "get_db", lambda: FakeDB())
    monkeypatch.setattr(
        app_module,
        "authenticate_ingest_client",
        lambda *_args: {"actor": "MACHINE_INGEST", "app_id": None, "default_repo": "owner/repo"},
    )

    def capture_incident(_db, _background_tasks, payload, actor, **kwargs):
        captured_incidents.append({"payload": payload, "actor": actor, "kwargs": kwargs})
        return "inc_error_1"

    monkeypatch.setattr(app_module, "ingest_machine_payload", capture_incident)
    client = TestClient(app_module.app)

    response = client.post(
        "/api/v1/logs",
        headers={"x-ingest-secret": "test-secret"},
        json=[
            {"service": "SAAPP", "level": "info", "message": "Started"},
            {"service": "SAAPP", "level": "warn", "message": "Slow request"},
            {
                "service": "SAAPP",
                "level": "error",
                "message": "Workflow failed\nTraceback: ValueError",
                "context": {"workflowName": "sonic_assistant", "node": "reasoner", "environment": "prod"},
            },
        ],
    )

    assert response.status_code == 202
    assert response.json()["incidentCount"] == 1
    assert response.json()["incidentIds"] == ["inc_error_1"]
    assert len(captured_incidents) == 1
    incident = captured_incidents[0]["payload"]
    assert incident["error_message"] == "Workflow failed"
    assert incident["stack_trace"] == "Workflow failed\nTraceback: ValueError"
    assert incident["environment"] == "prod"
    assert incident["metadata"]["source"] == "structured_log"
    assert incident["metadata"]["node"] == "reasoner"


def test_replay_tagged_log_is_persisted_and_replayed(monkeypatch):
    stored_logs = []

    class FakeCursor(list):
        def sort(self, *_args):
            return self

        def limit(self, count):
            return FakeCursor(self[:count])

    class FakeCollection:
        def insert_one(self, document):
            stored_logs.append(document)

        def find(self, query, *_args):
            requested_id = query["context.requestId"]
            matches = [
                document
                for document in stored_logs
                if document["context"].get("workflowName") == query["context.workflowName"]
                and (
                    isinstance(requested_id, dict)
                    and bool(document["context"].get("requestId"))
                    or document["context"].get("requestId") == requested_id
                )
            ]
            return FakeCursor(matches)

    class FakeDB:
        def __getitem__(self, _name):
            return FakeCollection()

    monkeypatch.setattr(app_module, "get_db", lambda: FakeDB())
    monkeypatch.setattr(
        app_module,
        "authenticate_ingest_client",
        lambda *_args: {"actor": "MACHINE_INGEST", "app_id": None, "default_repo": None},
    )
    app_module.app.dependency_overrides[app_module.get_current_user] = lambda: {"sub": "operator"}
    client = TestClient(app_module.app)

    try:
        ingest_response = client.post(
            "/api/v1/logs",
            headers={"x-ingest-secret": "test-secret"},
            json={
                "service": "SAAPP",
                "level": "info",
                "message": "Retriever completed",
                "context": {
                    "workflowName": "sonic_assistant",
                    "requestId": "req_test_123",
                    "node": "retriever",
                    "input": {"query": "test"},
                    "output": {"documents": 3},
                },
            },
        )
        assert ingest_response.status_code == 202
        assert ingest_response.json()["persistedReplayEvents"] == 1

        replay_response = client.post(
            "/api/v1/replay",
            json={"workflowName": "sonic_assistant", "requestId": "req_test_123"},
        )
        assert replay_response.status_code == 200
        assert replay_response.json()["timeline"][0]["node"] == "retriever"
        assert replay_response.json()["timeline"][0]["output"] == {"documents": 3}

        get_replay_response = client.get(
            "/api/v1/replay",
            params={"workflowName": "sonic_assistant", "requestId": "req_test_123"},
        )
        assert get_replay_response.status_code == 200
        assert get_replay_response.json()["timeline"] == replay_response.json()["timeline"]

        runs_response = client.get(
            "/api/v1/replay/runs",
            params={"workflowName": "sonic_assistant"},
        )
        assert runs_response.status_code == 200
        runs = runs_response.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["requestId"] == "req_test_123"
        assert runs[0]["nodeName"] == "retriever"
        assert runs[0]["nodeCount"] == 1
        assert isinstance(runs[0]["latestTimestamp"], str)
    finally:
        app_module.app.dependency_overrides.clear()
