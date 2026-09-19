"""The local-dev-mode daemon: ``erragent serve --root <path>``.

Binds to 127.0.0.1 only. Exposes a ``POST /api/v1/logs`` endpoint compatible with the same
``LogEventInput`` wire contract the cloud backend accepts, so ``ErrAgentHandler`` pointed at
``ERRAGENT_LOCAL_URL`` works completely unmodified. For error-level events, the daemon:

1. Resolves the stack trace to a real file under ``--root`` and reads it off disk.
2. Reports the incident to the *cloud* errAgent backend (with the file content inlined), since
   Gemini analysis and patch-safety validation stay server-side — the daemon never talks to
   Gemini or applies a patch itself.
3. Polls for the resulting ``local_patch`` proposal and, once ready, shows a terminal diff and
   waits for explicit approval before ever touching the filesystem.
4. On approval, re-verifies content hashes (defense in depth — never trusts the network
   response blindly) and the local file's current hash against the hash from analysis time
   (refuses to overwrite a file that changed since analysis), then does an atomic write.
5. Reports the outcome back to the cloud so the proposal/incident status reflects reality.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from ..client import _cloud_headers
from ..config import ErrAgentConfig, load_config
from .approve_cli import prompt_approval
from .stackwalk import resolve_target_file

logger = logging.getLogger("erragent.local.daemon")


class LogEvent(BaseModel):
    service: str
    level: str
    message: str
    timestamp: int | float | str | None = None
    context: dict[str, Any] = {}


def _cloud_request_sync(
    config: ErrAgentConfig, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    assert config.cloud is not None
    headers = _cloud_headers(config.cloud)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{config.cloud.url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=config.cloud.timeout_seconds) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


async def _cloud_request(config: ErrAgentConfig, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_cloud_request_sync, config, method, path, body)


def create_app(root: Path, poll_interval: float = 2.0, poll_timeout: float = 180.0) -> FastAPI:
    app = FastAPI(title="erragent local daemon")
    resolved_root = root.resolve()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "root": str(resolved_root)}

    @app.post("/api/v1/logs")
    async def ingest_log(payload: LogEvent | list[LogEvent]) -> dict[str, Any]:
        events = payload if isinstance(payload, list) else [payload]
        error_events = [event for event in events if event.level == "error"]
        for event in error_events:
            asyncio.create_task(
                _handle_error_event(resolved_root, event, poll_interval, poll_timeout)
            )
        return {"status": "accepted", "count": len(events), "errorCount": len(error_events)}

    return app


async def _handle_error_event(root: Path, event: LogEvent, poll_interval: float, poll_timeout: float) -> None:
    config = load_config(ignore_local_only=True)
    if config.cloud is None:
        logger.error(
            "No cloud credentials configured (ERRAGENT_URL + ERRAGENT_INGEST_SECRET or "
            "ERRAGENT_APP_ID/ERRAGENT_APP_SECRET) — cannot analyze this error."
        )
        return

    resolved = resolve_target_file(root, event.message, event.context)
    if resolved is None:
        logger.info("Could not resolve a local source file for this error; skipping local analysis.")
        return
    target_file_path, absolute_path = resolved

    try:
        file_content = absolute_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Failed to read %s: %s", absolute_path, exc)
        return

    incident_payload = {
        "service_name": event.service,
        "environment": "development",
        "error_message": event.message.splitlines()[0] if event.message else "Unhandled Exception",
        "stack_trace": event.message,
        "metadata": {
            **event.context,
            "source": "local_dev_daemon",
            "local_dev": True,
            "target_file_path": target_file_path,
            "inline_file_content": file_content,
            "inline_file_sha256": hashlib.sha256(file_content.encode("utf-8")).hexdigest(),
            "local_project_root": str(root),
        },
    }

    try:
        response = await _cloud_request(config, "POST", "/api/v1/webhooks/ingest", incident_payload)
    except Exception as exc:
        logger.error("Failed to report incident to errAgent: %s", exc)
        return

    incident_id = response.get("incident_id")
    if not incident_id:
        logger.error("errAgent did not return an incident_id: %s", response)
        return

    logger.info("Local error reported (incident %s). Analyzing...", incident_id)

    proposal = await _poll_for_proposal(config, incident_id, poll_interval, poll_timeout)
    if proposal is None or proposal.get("status") != "awaiting_approval":
        return

    # prompt_approval() blocks on input() — run it off the event loop so the daemon keeps
    # serving other requests (e.g. /health) while waiting on the developer's terminal. Its
    # diff display and y/N prompt are interactive UI, not log records, so it uses print()/
    # input() directly rather than the logger — everything else in this module goes through
    # `logger` so it's consistently controlled by the logging config set up in cli.py.
    approved = await asyncio.to_thread(
        prompt_approval,
        target_file_path=target_file_path,
        diff=proposal.get("action", {}).get("diff_preview", ""),
        pr_title=proposal.get("action", {}).get("pr_title", ""),
    )

    if not approved:
        try:
            await _cloud_request(config, "POST", f"/api/v1/local-patch/{proposal['_id']}/decline")
        except Exception as exc:
            logger.error("Failed to record decline: %s", exc)
        logger.info("Fix declined; no changes written.")
        return

    await _apply_proposal(config, root, target_file_path, absolute_path, proposal["_id"])


async def _poll_for_proposal(
    config: ErrAgentConfig, incident_id: str, poll_interval: float, poll_timeout: float
) -> dict[str, Any] | None:
    elapsed = 0.0
    while elapsed < poll_timeout:
        try:
            status = await _cloud_request(config, "GET", f"/api/v1/local-patch/by-incident/{incident_id}")
        except Exception as exc:
            logger.error("Failed to poll analysis status: %s", exc)
            return None

        if status.get("proposal"):
            return status["proposal"]
        if status.get("incident_status") == "analysis_failed":
            logger.error("Analysis failed: %s", status.get("failure_reason"))
            return None

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    logger.warning("Timed out waiting for analysis to finish.")
    return None


async def _ack(config: ErrAgentConfig, proposal_id: str, outcome: str, detail: str | None) -> None:
    try:
        await _cloud_request(
            config, "POST", f"/api/v1/local-patch/{proposal_id}/ack", {"outcome": outcome, "detail": detail}
        )
    except Exception as exc:
        logger.error("Failed to ack proposal %s: %s", proposal_id, exc)


async def _apply_proposal(
    config: ErrAgentConfig, root: Path, target_file_path: str, absolute_path: Path, proposal_id: str
) -> None:
    try:
        content = await _cloud_request(config, "POST", f"/api/v1/local-patch/{proposal_id}/approve")
    except Exception as exc:
        logger.error("Approval failed: %s", exc)
        return

    resolved_path = absolute_path.resolve()
    try:
        resolved_path.relative_to(root)
    except ValueError:
        logger.error("Refusing to apply: resolved path escaped the project root.")
        await _ack(config, proposal_id, "failed", "resolved path escaped the project root")
        return

    if content.get("content_source") != "sandbox_applied":
        logger.error("Refusing to apply: remediation content was not sandbox-verified.")
        await _ack(config, proposal_id, "failed", "remediation content was not sandbox-verified")
        return

    full_content = content.get("full_file_content") or ""
    expected_hash = content.get("full_file_content_sha256") or ""
    if hashlib.sha256(full_content.encode("utf-8")).hexdigest() != expected_hash:
        logger.error("Refusing to apply: content failed integrity check.")
        await _ack(config, proposal_id, "failed", "full_file_content failed integrity check")
        return

    try:
        current_content = absolute_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Could not re-read %s before writing: %s", absolute_path, exc)
        await _ack(config, proposal_id, "failed", f"could not re-read local file: {exc}")
        return

    base_hash = content.get("base_file_sha256") or ""
    if base_hash and hashlib.sha256(current_content.encode("utf-8")).hexdigest() != base_hash:
        logger.warning("File changed on disk since analysis — refusing to overwrite. Re-run to retry.")
        await _ack(config, proposal_id, "failed", "local file changed since analysis; refused to overwrite")
        return

    tmp_path = absolute_path.with_name(absolute_path.name + ".erragent-tmp")
    try:
        tmp_path.write_text(full_content, encoding="utf-8")
        os.replace(tmp_path, absolute_path)
    except OSError as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        logger.error("Failed to write %s: %s", target_file_path, exc)
        await _ack(config, proposal_id, "failed", f"write failed: {exc}")
        return

    await _ack(config, proposal_id, "succeeded", None)
    logger.info("Applied fix to %s.", target_file_path)
