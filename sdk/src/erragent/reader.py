"""Read an app's OWN incidents back from errAgent.

The rest of the SDK reports (writes) to errAgent. This is the other direction, for an app that wants to look at what errAgent
holds about itself (an admin tool, a status page, a "what broke overnight?" digest)::

    import erragent
    incidents = await erragent.list_incidents(since="24h")
    detail = await erragent.get_incident(incidents[0]["id"])
    deploy = await erragent.latest_deploy()

Configuration (environment only, like the rest of the SDK)::

    ERRAGENT_URL          the errAgent base URL (shared with reporting)
    ERRAGENT_APP_ID       this app's id (shared with reporting)
    ERRAGENT_READ_SECRET  this app's READ secret

The read secret is NOT the ingest secret. It is issued per app with ``python -m backend.scripts.manage_app_read_access`` in the
errAgent repo, stored there only as a hash, and scoped on the server to this app's team and service names, so it can never read
anything but this app's incidents. Treat it as a secret anyway: it is not for browsers or client bundles.

What comes back is deliberately small and content-free: ids, status, the first line of the error message, a truncated stack
trace, the AI analysis summary, fix status, and a short allowlist of operational metadata. Incident text is still data written by
whatever failed, including text a user typed, so a caller that hands it to a language model must treat it as untrusted input,
never as instructions.

Everything here is read-only, bounded (page size and look-back are capped on both ends), and never puts a secret in an error.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .client import ErrAgentNotConfigured
from .config import ReadConfig, load_read_config

MAX_LIMIT = 50


class ErrAgentReadError(RuntimeError):
    """A read failed. ``status_code`` is the HTTP status, or None when errAgent could not be reached at all."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _require_read_config() -> ReadConfig:
    config = load_read_config()
    if config is None:
        raise ErrAgentNotConfigured(
            "ERRAGENT_URL, ERRAGENT_APP_ID and ERRAGENT_READ_SECRET must be configured to read incidents from errAgent."
        )
    return config


def _headers(config: ReadConfig) -> dict[str, str]:
    return {"Accept": "application/json", "x-app-id": config.app_id, "x-read-secret": config.read_secret}


def _get_json_sync(*, url: str, headers: dict[str, str], timeout_seconds: float) -> Any:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(parsed.get("detail", "")) if isinstance(parsed, dict) else ""
        except (ValueError, OSError):
            pass
        raise ErrAgentReadError(f"errAgent returned HTTP {exc.code}" + (f": {detail[:200]}" if detail else ""), exc.code) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ErrAgentReadError("Could not reach errAgent.", None) from None
    try:
        return json.loads(body)
    except ValueError:
        raise ErrAgentReadError("errAgent returned a response that was not JSON.", None) from None


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    config = _require_read_config()
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{config.url.rstrip('/')}{path}" + (f"?{query}" if query else "")
    return await asyncio.to_thread(_get_json_sync, url=url, headers=_headers(config), timeout_seconds=config.timeout_seconds)


async def list_incidents(
    *,
    status: str | None = None,
    since: str | None = None,
    limit: int = 20,
    environment: str | None = "production",
) -> list[dict[str, Any]]:
    """This app's recent incidents, newest first. ``since`` is "30m", "24h", "7d" or an ISO timestamp (default a week, at most 30
    days). ``environment`` defaults to production; pass None to take the server's default. ``limit`` is clamped to 1..50."""
    payload = await _get(
        "/api/v1/app/incidents",
        {"status": status, "since": since, "limit": max(1, min(int(limit), MAX_LIMIT)), "environment": environment},
    )
    incidents = payload.get("incidents") if isinstance(payload, dict) else None
    return incidents if isinstance(incidents, list) else []


async def get_incident(incident_id: str) -> dict[str, Any]:
    """One of this app's incidents with its analysis. An id that isn't this app's is a 404, same as one that doesn't exist."""
    if not incident_id or not str(incident_id).strip():
        raise ValueError("incident_id is required")
    payload = await _get(f"/api/v1/app/incidents/{urllib.parse.quote(str(incident_id).strip(), safe='')}")
    incident = payload.get("incident") if isinstance(payload, dict) else None
    return incident if isinstance(incident, dict) else {}


async def list_logs(
    *,
    level: str | None = None,
    since: str | None = None,
    contains: str | None = None,
    request_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """This app's recent log lines, oldest first, from errAgent's live buffer. ``level`` is a minimum severity ("info", "warn" or
    "error"); ``since`` is "30m", "6h" or an ISO timestamp (default one hour); ``contains`` is a case-insensitive substring;
    ``request_id`` follows one request. ``limit`` is clamped to 1..100.

    The buffer lives in errAgent's memory, so it holds what arrived since errAgent last restarted, up to its size, not a full
    history. The result is ``{"entries": [...], "matched": n, "buffered": n, "oldest_buffered": timestamp}``, so a caller can tell
    "nothing matched" from "the buffer doesn't reach that far back"."""
    payload = await _get(
        "/api/v1/app/logs",
        {"level": level, "since": since, "contains": contains, "request_id": request_id, "limit": max(1, min(int(limit), 100))},
    )
    return payload if isinstance(payload, dict) else {}


async def latest_deploy() -> dict[str, Any]:
    """Status of this app's latest Render deploy, or ``{"status": "not_configured", ...}`` when no Render service is set up."""
    payload = await _get("/api/v1/app/deploy")
    return payload if isinstance(payload, dict) else {}
