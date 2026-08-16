import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

LogLevel = Literal["info", "warn", "error"]


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

    async def publish(self, event: LogEventInput, source_app_id: str | None = None) -> dict[str, Any]:
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

        for queue, (service, level) in subscribers:
            if service != entry["service"] or (level and level != entry["level"]):
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

    async def clear(self) -> None:
        async with self._lock:
            self._buffers.clear()
            self._subscribers.clear()


log_broker = LogBroker()
