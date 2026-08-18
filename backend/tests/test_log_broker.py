import asyncio
import logging

from fastapi.testclient import TestClient

import backend.app.app as app_module
from backend.app.logging.logger import NOISY_LOGGERS
from backend.services.log_broker import InternalLogHandler, LogBroker, LogEventInput
from backend.services.log_broker import log_broker


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
