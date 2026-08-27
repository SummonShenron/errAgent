import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from backend.app.logging.logger import NOISY_LOGGERS

LogLevel = Literal["info", "warn", "error"]

_INTERNAL_LOG_LEVELS: dict[int, LogLevel] = {
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}
_EXCLUDED_LOGGER_PREFIXES = NOISY_LOGGERS


class LogEventInput(BaseModel):
    service: str = Field(min_length=1, max_length=64)
    level: LogLevel
    message: str = Field(min_length=1, max_length=4000)
    timestamp: int | float | str | datetime | None = None
    context: dict[str, Any] = Field(default_factory=dict)


def _normalize_timestamp(value: int | float | str | datetime | None) -> str:
    if value is None:
        timestamp = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        timestamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LogBroker:
    def __init__(self, max_entries_per_service: int = 5000, subscriber_queue_size: int = 500):
        self._buffers: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=max_entries_per_service)
        )
        self._subscribers: dict[asyncio.Queue, tuple[str, str | None]] = {}
        self._subscriber_queue_size = subscriber_queue_size
        self._lock = asyncio.Lock()

    async def publish(
        self, 
        event: LogEventInput | str, 
        source_app_id: str | None = None,
        service: str = "patchy",
        level: LogLevel = "info",
    ) -> dict[str, Any]:
        # Handle raw strings by converting them to a LogEventInput model
        if isinstance(event, str):
            event = LogEventInput(
                service=service,
                level=level,
                message=event,
            )

        entry = {
            "id": uuid4().hex,
            "service": event.service.strip(),
            "level": event.level,
            "message": event.message.strip(),
            "timestamp": _normalize_timestamp(event.timestamp),
            "context": event.context,
        }
        if source_app_id:
            entry["source_app_id"] = source_app_id

        async with self._lock:
            self._buffers[entry["service"]].append(entry)
            subscribers = list(self._subscribers.items())

        for queue, (sub_service, sub_level) in subscribers:
            if sub_service != entry["service"] or (sub_level and sub_level != entry["level"]):
                continue
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(entry)

        return entry

    async def subscribe(
        self,
        service: str,
        level: str | None = None,
        history_limit: int = 500,
    ) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        queue = asyncio.Queue(maxsize=self._subscriber_queue_size)
        async with self._lock:
            history = list(self._buffers.get(service, ()))
            if level:
                history = [entry for entry in history if entry["level"] == level]
            history = history[-history_limit:]
            self._subscribers[queue] = (service, level)
        return queue, history

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.pop(queue, None)

    async def get_history(
        self,
        service: str | None = None,
        level: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            if service:
                history = list(self._buffers.get(service, ()))
            else:
                history = [entry for buffer in self._buffers.values() for entry in buffer]

        if level:
            history = [entry for entry in history if entry["level"] == level]
        history.sort(key=lambda entry: entry["timestamp"])
        return history[-max(1, min(limit, 200)):]

    async def clear(self) -> None:
        async with self._lock:
            self._buffers.clear()
            self._subscribers.clear()


class InternalLogHandler(logging.Handler):
    def __init__(self, broker: LogBroker, loop: asyncio.AbstractEventLoop):
        super().__init__(level=logging.INFO)
        self._broker = broker
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_EXCLUDED_LOGGER_PREFIXES):
            return

        try:
            message = record.getMessage()
            if record.exc_info:
                traceback = logging.Formatter().formatException(record.exc_info)
                message = f"{message}\n{traceback}"

            event = LogEventInput(
                service="errAgent",
                level=_INTERNAL_LOG_LEVELS.get(record.levelno, "info"),
                message=message,
                timestamp=record.created,
                context={
                    "logger": record.name,
                    "module": record.module,
                    "function": record.funcName,
                    "line": record.lineno,
                },
            )
            self._loop.call_soon_threadsafe(
                asyncio.create_task,
                self._broker.publish(event),
            )
        except Exception:
            pass


def install_internal_log_handler(
    broker: LogBroker,
    loop: asyncio.AbstractEventLoop,
    target_logger: logging.Logger | None = None,
) -> InternalLogHandler:
    root_logger = logging.getLogger()
    existing_handler = next(
        (handler for handler in root_logger.handlers if isinstance(handler, InternalLogHandler)),
        None,
    )
    handler = existing_handler or InternalLogHandler(broker, loop)
    if existing_handler is None:
        root_logger.addHandler(handler)

    if target_logger is not None and handler not in target_logger.handlers:
        target_logger.addHandler(handler)
    return handler


log_broker = LogBroker()
