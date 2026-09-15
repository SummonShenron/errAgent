"""Report a pre-formed incident or browser error directly, bypassing the logging-handler stream.

Two contracts, both "I already have a structured error, not a log line I want streamed":

- ``report_incident`` -> ``POST /api/v1/webhooks/ingest`` — a backend-originated error.
- ``report_client_error`` -> ``POST /api/v1/client-errors`` — a *sanitized* browser error
  forwarded through the target app's own backend proxy (never call this from browser code;
  errAgent ingestion credentials must never reach the browser).

This is the SDK's replacement for hand-building a custom ingestion client per target app (see
local-rag's/BTY's ``build_erragent_ingest_payload``/``post_erragent_ingest``/``dispatch_erragent_ingest``
and BTY's additional ``post_erragent_client_error``/``send_erragent_client_error``, all superseded
by this module).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from .config import load_config, resolve_cloud_credentials

_background_tasks: set[asyncio.Task[Any]] = set()


class ErrAgentNotConfigured(RuntimeError):
    """Raised when a report_* call is made without cloud credentials configured."""


def _cloud_headers(cloud) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    app_id, secret = resolve_cloud_credentials(cloud)
    if app_id:
        headers["x-app-id"] = app_id
    if secret:
        headers["x-ingest-secret"] = secret
    return headers


def _require_cloud_config():
    config = load_config()
    if config.cloud is None:
        raise ErrAgentNotConfigured(
            "ERRAGENT_URL and credentials (ERRAGENT_APP_ID/ERRAGENT_APP_SECRET or "
            "ERRAGENT_INGEST_SECRET) must be configured to report to errAgent."
        )
    return config.cloud


def _post_json_sync(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return {"status_code": response.getcode(), "body": response.read().decode("utf-8")}
    except urllib.error.HTTPError as exc:
        return {"status_code": exc.code, "body": exc.read().decode("utf-8", errors="replace")}


def _run_nowait(coro_fn, /, **kwargs: Any) -> None:
    """Schedule a fire-and-forget task, swallowing failures — for callers that can't await.

    Silently no-ops if there's no running event loop or errAgent isn't configured; this is
    meant for opportunistic reporting, not a guaranteed-delivery path.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _safe() -> None:
        try:
            await coro_fn(**kwargs)
        except Exception:
            pass

    task = loop.create_task(_safe())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def report_incident(
    *,
    error_message: str,
    stack_trace: str = "",
    service: str | None = None,
    environment: str | None = None,
    repository: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST a pre-formed incident to errAgent. Raises ``ErrAgentNotConfigured`` if unconfigured."""
    cloud = _require_cloud_config()

    payload: dict[str, Any] = {
        "service_name": service or cloud.service,
        "environment": environment or "production",
        "error_message": error_message,
        "stack_trace": stack_trace,
        "metadata": metadata or {},
    }
    if repository:
        payload["repository"] = repository

    endpoint = f"{cloud.url.rstrip('/')}/api/v1/webhooks/ingest"
    return await asyncio.to_thread(
        _post_json_sync,
        url=endpoint,
        headers=_cloud_headers(cloud),
        payload=payload,
        timeout_seconds=cloud.timeout_seconds,
    )


def report_incident_nowait(
    *,
    error_message: str,
    stack_trace: str = "",
    service: str | None = None,
    environment: str | None = None,
    repository: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget variant of ``report_incident`` for callers that can't await."""
    _run_nowait(
        report_incident,
        error_message=error_message,
        stack_trace=stack_trace,
        service=service,
        environment=environment,
        repository=repository,
        metadata=metadata,
    )


async def report_client_error(
    *,
    message: str,
    service: str | None = None,
    environment: str = "production",
    release: str | None = None,
    route: str | None = None,
    source: str = "frontend",
    stack: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST a sanitized browser error to errAgent's ``/api/v1/client-errors`` proxy contract.

    Call this from the target app's *backend* only, after sanitizing the payload — never from
    browser code, since that would expose errAgent ingestion credentials to the client.
    """
    cloud = _require_cloud_config()

    payload: dict[str, Any] = {
        "service": service or cloud.service,
        "environment": environment,
        "release": release,
        "route": route,
        "source": source,
        "message": message,
        "stack": stack,
        "metadata": metadata or {},
    }

    endpoint = f"{cloud.url.rstrip('/')}/api/v1/client-errors"
    return await asyncio.to_thread(
        _post_json_sync,
        url=endpoint,
        headers=_cloud_headers(cloud),
        payload=payload,
        timeout_seconds=cloud.timeout_seconds,
    )


def report_client_error_nowait(
    *,
    message: str,
    service: str | None = None,
    environment: str = "production",
    release: str | None = None,
    route: str | None = None,
    source: str = "frontend",
    stack: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget variant of ``report_client_error`` for callers that can't await."""
    _run_nowait(
        report_client_error,
        message=message,
        service=service,
        environment=environment,
        release=release,
        route=route,
        source=source,
        stack=stack,
        metadata=metadata,
    )
