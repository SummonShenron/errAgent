import logging

from erragent.context import context
from erragent.handler import ErrAgentHandler


def _make_handler() -> tuple[ErrAgentHandler, list[dict]]:
    handler = ErrAgentHandler(
        erragent_url="http://example.invalid",
        service="test-service",
        ingest_secret="secret",
    )
    captured: list[dict] = []
    handler._deliver_with_retries = captured.append  # type: ignore[assignment]
    return handler, captured


def _record(level: int, message: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test-logger",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
        func="test_func",
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_emit_builds_expected_payload_shape():
    handler, captured = _make_handler()
    handler.emit(_record(logging.ERROR, "boom"))
    handler._queue.join()

    assert len(captured) == 1
    payload = captured[0]
    assert payload["service"] == "test-service"
    assert payload["level"] == "error"
    assert payload["message"] == "boom"
    assert payload["context"]["logger"] == "test-logger"
    assert payload["context"]["function"] == "test_func"


def test_emit_merges_active_erragent_context():
    handler, captured = _make_handler()
    with context(workflow_name="ingest", node="fetch"):
        handler.emit(_record(logging.INFO, "starting"))
    handler._queue.join()

    payload = captured[0]
    assert payload["context"]["workflow_name"] == "ingest"
    assert payload["context"]["node"] == "fetch"
    assert payload["level"] == "info"


def test_per_call_erragent_context_overrides_active_context():
    handler, captured = _make_handler()
    with context(node="outer"):
        handler.emit(_record(logging.WARNING, "careful", erragent_context={"node": "inner"}))
    handler._queue.join()

    assert captured[0]["context"]["node"] == "inner"
    assert captured[0]["level"] == "warn"
