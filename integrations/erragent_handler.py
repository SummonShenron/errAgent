import json
import logging
import os
import queue
import threading
import urllib.request
from typing import Any


_LEVELS = {
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


def _json_fallback(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")

    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()

    return str(value)


class ErrAgentHandler(logging.Handler):
    def __init__(
        self,
        erragent_url: str,
        ingest_secret: str,
        service: str,
        timeout_seconds: float = 30.0,
        queue_size: int = 1000,
        max_delivery_attempts: int = 4,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        super().__init__(level=logging.INFO)
        self.endpoint = f"{erragent_url.rstrip('/')}/api/v1/logs"
        self.ingest_secret = ingest_secret
        self.service = service
        self.timeout_seconds = timeout_seconds
        self.max_delivery_attempts = max(1, max_delivery_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=queue_size)
        self._worker = threading.Thread(
            target=self._send_loop,
            name="erragent-log-forwarder",
            daemon=True,
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            context = {
                "logger": record.name,
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            }
            custom_context = getattr(record, "erragent_context", None)
            if isinstance(custom_context, dict):
                context.update(custom_context)

            message = record.getMessage()
            if record.exc_info:
                traceback = logging.Formatter().formatException(record.exc_info)
                message = f"{message}\n{traceback}"

            payload = {
                "service": self.service,
                "level": _LEVELS.get(record.levelno, "info"),
                "message": message,
                "timestamp": int(record.created * 1000),
                "context": context,
            }
            self._queue.put_nowait(payload)
        except queue.Full:
            pass
        except Exception:
            self.handleError(record)

    def _send_loop(self) -> None:
        while True:
            payload = self._queue.get()
            try:
                self._deliver_with_retries(payload)
            finally:
                self._queue.task_done()

    def _deliver_with_retries(self, payload: dict[str, Any]) -> None:
        for attempt in range(self.max_delivery_attempts):
            try:
                request = urllib.request.Request(
                    self.endpoint,
                    data=json.dumps(payload, default=_json_fallback).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "x-ingest-secret": self.ingest_secret,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=self.timeout_seconds):
                    return
            except Exception:
                if attempt + 1 < self.max_delivery_attempts:
                    delay = self.retry_delay_seconds * (2**attempt)
                    if delay:
                        threading.Event().wait(delay)

    def handleError(self, record: logging.LogRecord) -> None:
        pass


def install_erragent_logging(logger: logging.Logger | None = None) -> bool:
    erragent_url = os.getenv("ERRAGENT_URL")
    ingest_secret = os.getenv("ERRAGENT_INGEST_SECRET")
    service = os.getenv("ERRAGENT_SERVICE")
    timeout_seconds = float(os.getenv("ERRAGENT_TIMEOUT_SECONDS", "30"))
    if not erragent_url or not ingest_secret or not service:
        return False

    target_logger = logger or logging.getLogger()
    if any(isinstance(handler, ErrAgentHandler) for handler in target_logger.handlers):
        return True

    target_logger.addHandler(
        ErrAgentHandler(
            erragent_url=erragent_url,
            ingest_secret=ingest_secret,
            service=service,
            timeout_seconds=timeout_seconds,
        )
    )
    if target_logger.level == logging.NOTSET or target_logger.level > logging.INFO:
        target_logger.setLevel(logging.INFO)
    return True
