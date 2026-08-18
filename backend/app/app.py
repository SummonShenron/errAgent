import os
import hmac
import json
import hashlib
import logging
import asyncio
from uuid import uuid4
from typing import List, Dict, Any
from datetime import datetime, timezone, timedelta, time
from bson import ObjectId
import requests
from fastapi import Body, FastAPI, HTTPException, Depends, status, BackgroundTasks, Header, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError
from backend.schemas.incident_schemas import IncidentCreate, IncidentInDB, IncidentStatus, AuditLogEntry
# Utility imports from your backend/utils directory
from backend.utils.db_utils import get_db
from backend.utils.app_utils import (
    SERVICES,
    build_health_report,
    require_sentry_secret,
    resolve_commit_file_content,
    run_ai_analysis_pipeline,
    run_service_health_checks,
    serialize_mongo_doc,
    ingest_machine_payload,
    authenticate_ingest_client,
    is_synthetic_debug_incident,
    debug_suppression_bypassed,
)
from backend.utils.isolation_auth import decode_access_token, get_current_user
from backend.services.github_service import GitHubOpsService
from backend.services.log_broker import InternalLogHandler, LogEventInput, install_internal_log_handler, log_broker
from backend.middleware.rbac import require_role

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ErrAgent Logger")

app = FastAPI(title="errAgent Incident Engine", version="1.0.0")
health_monitor_task: asyncio.Task | None = None
internal_log_handler: InternalLogHandler | None = None

github_service = GitHubOpsService()
SENTRY_WEBHOOK_SECRET = os.getenv("SENTRY_WEBHOOK_SECRET")
# Backward-compatible legacy shared secret for existing app-to-app clients.
INGEST_WEBHOOK_SECRET = os.getenv("INGEST_WEBHOOK_SECRET") or os.getenv("ERRAGENT_INGEST_SECRET")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DEFAULT_TARGET_REPO = os.getenv("DEFAULT_TARGET_REPO", "SummonShenron/SAAPP")
SUPPRESS_DEBUG_INCIDENTS = os.getenv("SUPPRESS_DEBUG_INCIDENTS", "false").lower() in {"1", "true", "yes", "on"}
INCIDENT_DEDUPE_WINDOW_SECONDS = int(os.getenv("INCIDENT_DEDUPE_WINDOW_SECONDS", "600"))
HEALTH_CHECK_INTERVAL_SECONDS = int(os.getenv("HEALTH_CHECK_INTERVAL_SECONDS", "300"))
HEALTH_SNAPSHOT_RETENTION_DAYS = int(os.getenv("HEALTH_SNAPSHOT_RETENTION_DAYS", "14"))
LAST_ALERTED_DOWN_SERVICES: set[str] = set()


def should_send_discord_alert(report: dict, already_alerted: bool = False) -> bool:
    if already_alerted:
        return False
    if report.get("overall_status") != "CRITICAL":
        return False
    services = report.get("services", [])
    return any(service.get("status") == "down" for service in services)


def build_discord_alert_message(report: dict) -> str:
    down_services = [
        service.get("service")
        for service in report.get("services", [])
        if service.get("status") == "down"
    ]
    service_list = ", ".join(down_services) if down_services else "unknown service"
    return (
        "@everyone errAgent health alert\n"
        f"Status: {report.get('overall_status', 'UNKNOWN')}\n"
        f"Down services: {service_list}\n"
        f"Summary: {report.get('summary', 'No summary available')}"
    )


def send_discord_alert(report: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL is not configured. Skipping Discord alert.")
        return False

    if not should_send_discord_alert(report, False):
        return False

    message = build_discord_alert_message(report)
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        response.raise_for_status()
        logger.warning("Discord alert sent for down services: %s", message)
        return True
    except Exception as exc:
        logger.exception("Failed to send Discord health alert: %s", exc)
        return False


class ReanalyzeRequest(BaseModel):
    instructions: str = ""

# Enable CORS for Frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def cleanup_old_health_snapshots() -> None:
    db = get_db()
    if db is None:
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=HEALTH_SNAPSHOT_RETENTION_DAYS)
    deleted = db["health_snapshots"].delete_many({"timestamp": {"$lt": cutoff}})
    if deleted.deleted_count:
        logger.info("Deleted %s old health snapshot records older than %s days", deleted.deleted_count, HEALTH_SNAPSHOT_RETENTION_DAYS)

async def health_monitor_loop() -> None:
    logger.info("Starting scheduled health monitor loop every %s seconds", HEALTH_CHECK_INTERVAL_SECONDS)
    monitoring_initialized = False
    while True:
        try:
            results = run_service_health_checks()
            report = build_health_report(results)
            logger.info("Scheduled health check complete: %s", report["overall_status"])

            down_services = {service["service"] for service in report.get("services", []) if service.get("status") == "down"}
            if monitoring_initialized and down_services and not down_services.issubset(LAST_ALERTED_DOWN_SERVICES):
                if send_discord_alert(report):
                    LAST_ALERTED_DOWN_SERVICES.update(down_services)
            elif not down_services:
                LAST_ALERTED_DOWN_SERVICES.clear()

            monitoring_initialized = True

            db = get_db()
            if db is not None:
                db["health_snapshots"].insert_one({
                    "timestamp": datetime.now(timezone.utc),
                    "overall_status": report["overall_status"],
                    "services": report["services"],
                    "summary": report["summary"],
                })
                await cleanup_old_health_snapshots()
        except Exception as exc:
            logger.exception("Scheduled health check failed: %s", exc)

        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

@app.on_event("startup")
async def startup_event() -> None:
    global health_monitor_task, internal_log_handler
    internal_log_handler = install_internal_log_handler(
        log_broker,
        asyncio.get_running_loop(),
        target_logger=logger,
    )
    logger.info("errAgent internal log streaming enabled")
    health_monitor_task = asyncio.create_task(health_monitor_loop())

@app.on_event("shutdown")
async def shutdown_event() -> None:
    global health_monitor_task, internal_log_handler
    if health_monitor_task is not None:
        health_monitor_task.cancel()
        try:
            await health_monitor_task
        except asyncio.CancelledError:
            logger.info("Health monitor loop cancelled during shutdown")
    if internal_log_handler is not None:
        logging.getLogger().removeHandler(internal_log_handler)
        logger.removeHandler(internal_log_handler)
        internal_log_handler = None

# --- 1. HEALTH CHECK ENDPOINTS ---
@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "service": "ErrAgent Backend Engine"}

@app.post("/api/v1/health/check")
def manual_health_check(payload: dict = Body(...)):
    logger.info("Received payload: %s", payload)

    service_name = payload.get("service")
    logger.info("Service name: %s", service_name)

    logger.info("SERVICES registry: %s", SERVICES)

    if service_name == "all":
        results = run_service_health_checks()
        return {"results": results}

    # single service
    svc = next((s for s in SERVICES if s["name"] == service_name), None)
    if not svc:
        raise HTTPException(status_code=404, detail="Service not found")

    # run single check
    single_results = run_service_health_checks()
    filtered = [r for r in single_results if r["service"] == service_name]

    return {"results": filtered}


@app.get("/api/v1/health/full", tags=["Health"])
def full_health_check():
    db = get_db()

    results = run_service_health_checks()
    report = build_health_report(results)

    # store snapshot in Mongo
    if db:
        db["health_snapshots"].insert_one({
            "timestamp": datetime.now(timezone.utc),
            "overall_status": report["overall_status"],
            "services": report["services"]
        })

    return report

@app.get("/api/v1/health/services")
def list_services():
    db = get_db()
    latest_snapshot = None
    latest_status_by_service: dict[str, dict[str, Any]] = {}

    if db is not None:
        latest_snapshot = db["health_snapshots"].find_one({}, sort=[("timestamp", -1)])
        if isinstance(latest_snapshot, dict):
            for service in latest_snapshot.get("services", []):
                if isinstance(service, dict) and service.get("service"):
                    latest_status_by_service[service["service"]] = service

    services_payload = []
    for svc in SERVICES:
        entry = dict(svc)
        last_status = latest_status_by_service.get(svc["name"], {})
        last_ts = latest_snapshot.get("timestamp") if isinstance(latest_snapshot, dict) else None
        entry["status"] = last_status.get("status", "unknown")
        entry["latency_ms"] = last_status.get("latency_ms")
        entry["http_status"] = last_status.get("http_status")
        entry["last_checked_at"] = last_ts.isoformat() if isinstance(last_ts, datetime) else last_ts
        services_payload.append(entry)

    return {"services": services_payload}

@app.get("/api/v1/events")
async def incident_events():
    async def event_stream():
        while True:
            try:
                yield "event: ping\ndata: {\"ts\": \"%s\"}\n\n" % datetime.now(timezone.utc).isoformat()
            except asyncio.CancelledError:
                logger.info("Event stream closed")
                break
            await asyncio.sleep(15)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/v1/logs", status_code=status.HTTP_202_ACCEPTED, tags=["Logs"])
async def ingest_logs(
    payload: LogEventInput | list[LogEventInput],
    background_tasks: BackgroundTasks,
    x_ingest_secret: str | None = Header(default=None),
    x_app_id: str | None = Header(default=None),
):
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")
    ingest_context = authenticate_ingest_client(db, x_ingest_secret, x_app_id)
    events = payload if isinstance(payload, list) else [payload]
    if len(events) > 100:
        raise HTTPException(status_code=413, detail="Log batches are limited to 100 entries")

    persisted_replay_events = 0
    incident_ids: list[str] = []
    for event in events:
        entry = await log_broker.publish(event, source_app_id=ingest_context.get("app_id"))
        context = event.context
        if all(context.get(field) for field in ("workflowName", "requestId", "node")):
            db["logs"].insert_one({
                **entry,
                "received_at": datetime.now(timezone.utc),
            })
            persisted_replay_events += 1

        if event.level == "error":
            metadata = dict(context)
            metadata.update({
                "source": "structured_log",
                "log_level": event.level,
                "log_timestamp": entry["timestamp"],
            })
            incident_payload = {
                "service_name": event.service,
                "environment": context.get("environment", "production"),
                "error_message": event.message.splitlines()[0],
                "stack_trace": event.message,
                "metadata": metadata,
            }
            repository = context.get("repository")
            if isinstance(repository, str) and repository.strip():
                incident_payload["repository"] = repository.strip()

            incident_ids.append(
                ingest_machine_payload(
                    db,
                    background_tasks,
                    incident_payload,
                    ingest_context["actor"],
                    app_id=ingest_context.get("app_id"),
                    app_default_repo=ingest_context.get("default_repo"),
                )
            )

    return {
        "status": "accepted",
        "count": len(events),
        "persistedReplayEvents": persisted_replay_events,
        "incidentCount": len(set(incident_ids)),
        "incidentIds": list(dict.fromkeys(incident_ids)),
    }


@app.websocket("/api/v1/live-logs")
async def live_logs(
    websocket: WebSocket,
    service: str = Query(..., min_length=1, max_length=64),
    level: str | None = Query(default=None),
    history: int = Query(default=500, ge=1, le=5000),
):
    await websocket.accept()
    if level not in {None, "info", "warn", "error"}:
        await websocket.close(code=4400, reason="Invalid log level")
        return

    try:
        auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        if auth_message.get("type") != "auth" or not auth_message.get("token"):
            raise HTTPException(status_code=401, detail="Authentication required")
        decode_access_token(str(auth_message["token"]))
    except WebSocketDisconnect:
        return
    except (HTTPException, asyncio.TimeoutError, ValueError, TypeError):
        await websocket.close(code=4401, reason="Authentication failed")
        return

    queue, history_entries = await log_broker.subscribe(service, level, history)
    try:
        await websocket.send_json({"type": "history", "entries": history_entries})
        while True:
            entry = await queue.get()
            await websocket.send_json({"type": "log", "entry": entry})
    except WebSocketDisconnect:
        pass
    finally:
        await log_broker.unsubscribe(queue)

# --- REPLAY ENDPOINT ---
@app.post("/api/v1/replay", tags=["Replay"])
async def replay_workflow(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """
    Replays a workflow execution using logged node inputs.
    Required fields:
      - workflowName
      - requestId
    """
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")

    workflow_name = payload.get("workflowName")
    request_id = payload.get("requestId")

    if not workflow_name or not request_id:
        raise HTTPException(status_code=400, detail="workflowName and requestId are required")

    # 1. Fetch logs for this workflow run
    logs = list(
        db["logs"].find({
            "context.workflowName": workflow_name,
            "context.requestId": request_id
        }).sort("timestamp", 1)
    )

    if not logs:
        raise HTTPException(status_code=404, detail="No logs found for this workflow run")

    # 2. Build replay timeline
    timeline = []
    for entry in logs:
        ctx = entry.get("context", {})
        timeline.append({
            "node": ctx.get("node") or "unknown-node",
            "input": ctx.get("input") if isinstance(ctx.get("input"), dict) else {},
            "output": ctx.get("output") if isinstance(ctx.get("output"), dict) else {},
            "timestamp": entry.get("timestamp")
        })

    # 3. Return replay timeline
    return {
        "workflowName": workflow_name,
        "requestId": request_id,
        "timeline": timeline
    }

# --- SAAPP Integration Endpoints ---
@app.get("/ops/context")
async def get_ops_context():
    return {
        "incidents": [],
        "health": [],
        "warmingEvents": [],
        "latency": [],
        "deploys": [],
        "riskScore": 0.0
    }

@app.get("/api/v1/replay", tags=["Replay"])
async def get_replay(
    workflowName: str = Query(..., min_length=1),
    requestId: str = Query(..., min_length=1),
    current_user: dict = Depends(get_current_user),
):
    """Retrieves a persisted workflow timeline using query parameters."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")

    logs = list(
        db["logs"].find({
            "context.workflowName": workflowName,
            "context.requestId": requestId,
        }).sort("timestamp", 1)
    )
    if not logs:
        raise HTTPException(status_code=404, detail="No logs found for this workflow run")

    timeline = []
    for entry in logs:
        context = entry.get("context", {})
        timeline.append({
            "node": context.get("node") or "unknown-node",
            "input": context.get("input") if isinstance(context.get("input"), dict) else {},
            "output": context.get("output") if isinstance(context.get("output"), dict) else {},
            "timestamp": entry.get("timestamp"),
        })

    return {
        "workflowName": workflowName,
        "requestId": requestId,
        "timeline": timeline,
    }


@app.get("/api/v1/replay/runs", tags=["Replay"])
async def list_replay_runs(
    workflowName: str = Query(..., min_length=1),
    current_user: dict = Depends(get_current_user),
):
    """Lists recent persisted request IDs for a workflow."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")

    entries = list(
        db["logs"].find(
            {
                "context.workflowName": workflowName,
                "context.requestId": {"$exists": True, "$ne": ""},
            },
            {
                "_id": 0,
                "context.requestId": 1,
                "context.node": 1,
                "timestamp": 1,
            },
        ).sort("timestamp", -1).limit(1000)
    )

    runs: dict[str, dict[str, Any]] = {}
    for entry in entries:
        context = entry.get("context", {})
        request_id = context.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            continue
        run = runs.setdefault(
            request_id,
            {
                "requestId": request_id,
                "nodeName": context.get("node") or "unknown-node",
                "latestTimestamp": entry.get("timestamp"),
                "nodeCount": 0,
            },
        )
        run["nodeCount"] += 1

    return {"workflowName": workflowName, "runs": list(runs.values())}


# --- 2. LIST ALL INCIDENTS ---
@app.get("/api/v1/incidents", response_model=List[Dict[str, Any]], tags=["Incidents"])
async def list_incidents(current_user: dict = Depends(get_current_user)):
    """Fetches all incidents from MongoDB, ordered by most recent."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")
    
    incidents = list(db["incidents"].find({}).sort("created_at", -1))
    return serialize_mongo_doc(incidents)


# --- 3. GET SINGLE INCIDENT DETAILS ---
@app.get("/api/v1/incidents/{incident_id}", tags=["Incidents"])
async def get_incident_detail(incident_id: str, current_user: dict = Depends(get_current_user)):
    """
    Joins and returns the complete context for an incident:
    Raw Error Log + AI Root Cause Analysis + Pending Remediation Action
    """
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")

    incident = db["incidents"].find_one({"_id": incident_id})
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")

    analysis = db["analyses"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}

    return {
        "incident": serialize_mongo_doc(incident),
        "analysis": serialize_mongo_doc(analysis),
        "remediation": serialize_mongo_doc(remediation)
    }


# --- 4. INGEST NEW ERROR INCIDENT ---
@app.post("/api/v1/incidents", status_code=status.HTTP_201_CREATED, tags=["Incidents"])
async def ingest_incident(
    payload: Dict[str, Any], 
    background_tasks: BackgroundTasks,  # Injected background task runner
    current_user: dict = Depends(get_current_user)
):
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")
    incident_id = ingest_machine_payload(
        db,
        background_tasks,
        payload,
        current_user.get("username", "INGESTION_WEBHOOK"),
    )

    return {"status": "created", "incident_id": incident_id}

@app.delete("/api/v1/incidents/{incident_id}", tags=["Incidents"])
async def delete_incident(
    incident_id: str, 
    current_user: dict = Depends(get_current_user)
):
    """Deletes an incident and its associated records from MongoDB."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")

    # 1. Delete primary incident document
    result = db["incidents"].delete_one({"_id": incident_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Incident not found.")

    # 2. Cascade cleanup for linked AI analyses and remediations
    db["analyses"].delete_one({"incident_id": incident_id})
    db["remediations"].delete_one({"incident_id": incident_id})

    # 3. Log audit event
    actor = current_user.get("username", "operator")
    db["audit_logs"].insert_one({
        "incident_id": incident_id,
        "actor": actor,
        "action": "INCIDENT_DISMISSED",
        "timestamp": datetime.now(timezone.utc)
    })

    return {"status": "deleted", "incident_id": incident_id}

# --- 5. APPROVE AND EXECUTE HOTFIX PR ---
@app.post("/api/v1/incidents/{incident_id}/approve-hotfix", tags=["Incidents"])
async def approve_and_execute_hotfix(
    incident_id: str, 
    current_user: dict = Depends(require_role("Incident_Managers"))
):
    """
    Human-In-The-Loop action endpoint:
    Triggers GitHub PR creation and updates MongoDB states.
    """
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")

    actor_username = current_user.get("username", "admin")

    # 1. Fetch pending remediation doc
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)])
    if not remediation:
        raise HTTPException(status_code=404, detail="No remediation draft found for this incident.")

    if remediation.get("status") == "executed":
        return {
            "status": "already_executed",
            "message": "Hotfix PR has already been created on GitHub.",
            "pr_url": remediation.get("pr_url")
        }

    # 2. Execute GitHub API call
    repo = remediation["target_repo"]
    title = remediation["pr_title"]
    body = remediation["pr_body"]
    head = remediation["head_branch"]
    base = remediation["base_branch"]
    
    # Always commit full file content and reject diff-like payloads.
    file_path = remediation.get("target_file_path", "main.py")
    file_content = resolve_commit_file_content(remediation)
    file_content_bytes = len(file_content.encode("utf-8"))
    file_content_sha256 = hashlib.sha256(file_content.encode("utf-8")).hexdigest()

    logger.info(
        "Creating branch %s and pushing commit for %s (incident=%s, remediation_id=%s, bytes=%s, sha256=%s)",
        head,
        repo,
        incident_id,
        remediation.get("_id"),
        file_content_bytes,
        file_content_sha256,
    )

    db["audit_logs"].insert_one({
        "incident_id": incident_id,
        "actor": actor_username,
        "action": "HOTFIX_COMMIT_PAYLOAD_SELECTED",
        "details": {
            "remediation_id": str(remediation.get("_id")),
            "target_repo": repo,
            "target_file_path": file_path,
            "full_file_content_bytes": file_content_bytes,
            "full_file_content_sha256": file_content_sha256,
            "content_source": remediation.get("content_source"),
        },
        "timestamp": datetime.now(timezone.utc),
    })
    
    # Step A: Create the branch and commit the code fix first
    commit_result = await github_service.create_branch_and_commit(
        repo=repo,
        base_branch=base,
        new_branch=head,
        file_path=file_path,
        file_content=file_content,
        expected_base_file_sha256=remediation.get("base_file_sha256"),
        expected_full_file_sha256=remediation.get("full_file_content_sha256"),
        commit_message=f"Fix incident: {title}"
    )

    logger.info(f"Opening GitHub PR for {repo} ({head} -> {base})...")
    
    # Step B: Open the Pull Request successfully
    gh_response = await github_service.create_pull_request(
        repo=repo, title=title, body=body, head=head, base=base
    )

    if gh_response.get("status_code") not in [200, 201]:
        error_msg = gh_response.get("data", {}).get("message", "Failed to create PR on GitHub.")
        raise HTTPException(status_code=400, detail=f"GitHub API Error: {error_msg}")

    pr_url = gh_response["data"].get("html_url")

    # 3. Update Database states upon success
    now = datetime.now(timezone.utc)
    
    db["remediations"].update_one(
        {"incident_id": incident_id},
        {"$set": {
            "status": "executed",
            "approved_by": actor_username,
            "pr_url": pr_url,
            "commit_sha": commit_result.get("new_commit_sha"),
            "branch_updated": commit_result.get("branch_updated"),
            "updated_at": now,
        }}
    )
    
    db["incidents"].update_one(
        {"_id": incident_id},
        {"$set": {"status": "validating", "updated_at": now}}
    )

    # 4. Audit Log
    db["audit_logs"].insert_one({
        "incident_id": incident_id,
        "actor": actor_username,
        "action": "HOTFIX_APPROVED_AND_EXECUTED",
        "details": {
            "pr_url": pr_url,
            "commit_sha": commit_result.get("new_commit_sha"),
            "branch_name": commit_result.get("branch_name"),
        },
        "timestamp": now
    })

    return {
        "status": "success",
        "message": "Hotfix Pull Request opened successfully!",
        "pr_url": pr_url
    }

# --- 5A. APPROVE & CREATE GITHUB PR (Stage 1) ---
@app.post("/api/v1/incidents/{incident_id}/approve-hotfix", tags=["Incidents"])
async def approve_and_create_pr(
    incident_id: str, 
    current_user: dict = Depends(require_role("Incident_Managers"))
):
    db = get_db()
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)])
    if not remediation:
        raise HTTPException(status_code=404, detail="No remediation draft found.")

    repo = remediation["target_repo"]
    title = remediation["pr_title"]
    body = remediation["pr_body"]
    head = remediation["head_branch"]
    base = remediation["base_branch"]
    file_path = remediation.get("target_file_path", "main.py")
    
    # Always commit full file content and reject diff-like payloads.
    file_content = resolve_commit_file_content(remediation)
    file_content_bytes = len(file_content.encode("utf-8"))
    file_content_sha256 = hashlib.sha256(file_content.encode("utf-8")).hexdigest()

    logger.info(
        "Creating stage-1 branch %s for %s (incident=%s, remediation_id=%s, bytes=%s, sha256=%s)",
        head,
        repo,
        incident_id,
        remediation.get("_id"),
        file_content_bytes,
        file_content_sha256,
    )

    # 1. Push branch & commit full file content
    commit_result = await github_service.create_branch_and_commit(
        repo=repo, base_branch=base, new_branch=head, 
        file_path=file_path,
        file_content=file_content,
        expected_base_file_sha256=remediation.get("base_file_sha256"),
        expected_full_file_sha256=remediation.get("full_file_content_sha256"),
        commit_message=f"Fix incident: {title}"
    )

    # 2. Open PR
    gh_response = await github_service.create_pull_request(repo=repo, title=title, body=body, head=head, base=base)
    if gh_response.get("status_code") not in [200, 201]:
        error_msg = gh_response.get("data", {}).get("message", "Failed to create PR.")
        raise HTTPException(status_code=400, detail=f"GitHub API Error: {error_msg}")

    pr_data = gh_response["data"]
    pr_url = pr_data.get("html_url")
    pr_number = pr_data.get("number")
    now = datetime.now(timezone.utc)

    db["remediations"].update_one(
        {"incident_id": incident_id},
        {"$set": {
            "status": "pr_created", 
            "pr_url": pr_url, 
            "pr_number": pr_number, 
            "commit_sha": commit_result.get("new_commit_sha"),
            "branch_updated": commit_result.get("branch_updated"),
            "approved_by": current_user.get("username"),
            "updated_at": now
        }}
    )
    db["incidents"].update_one({"_id": incident_id}, {"$set": {"status": "fix_proposed", "updated_at": now}})

    return {"status": "success", "message": "PR created successfully!", "pr_url": pr_url}

# --- 5B. MERGE PR INTO MAIN (Stage 2 - HITL Final Step) ---
@app.post("/api/v1/incidents/{incident_id}/merge-hotfix", tags=["Incidents"])
async def merge_hotfix_pr(
    incident_id: str, 
    current_user: dict = Depends(require_role("Incident_Managers"))
):
    db = get_db()
    remediation = db["remediations"].find_one(
        {
            "incident_id": incident_id,
            "pr_number": {"$exists": True, "$ne": None},
        },
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if not remediation:
        remediation = db["remediations"].find_one(
            {
                "incident_id": incident_id,
                "status": {"$in": ["pr_created", "executed"]},
            },
            sort=[("updated_at", -1), ("created_at", -1)],
        )
    if not remediation:
        raise HTTPException(status_code=404, detail="No remediation found for this incident.")

    pr_number = remediation.get("pr_number")
    pr_url = remediation.get("pr_url")
    if not pr_number:
        head_branch = remediation.get("head_branch")
        repo = remediation.get("target_repo")
        if head_branch and repo:
            pr_lookup = await github_service.find_open_pull_request(repo=repo, head=head_branch)
            if pr_lookup.get("status_code") == 200:
                pr_data = pr_lookup["data"]
                pr_number = pr_data.get("number")
                pr_url = pr_data.get("html_url")
                db["remediations"].update_one(
                    {"_id": remediation.get("_id")},
                    {"$set": {
                        "pr_number": pr_number,
                        "pr_url": pr_url,
                        "status": remediation.get("status") or "pr_created",
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )

    if not pr_number:
        raise HTTPException(status_code=404, detail="No active PR found to merge. The remediation does not have a PR number and no open PR could be found for its branch.")

    repo = remediation["target_repo"]
    

    # Execute Merge via GitHub API
    merge_response = await github_service.merge_pull_request(repo=repo, pull_number=pr_number)
    if merge_response.get("status_code") not in [200, 201]:
        error_msg = merge_response.get("data", {}).get("message", "Merge failed.")
        raise HTTPException(status_code=400, detail=f"GitHub Merge Error: {error_msg}")

    now = datetime.now(timezone.utc)
    db["remediations"].update_one({"incident_id": incident_id}, {"$set": {"status": "merged", "updated_at": now}})
    db["incidents"].update_one({"_id": incident_id}, {"$set": {"status": "resolved", "updated_at": now}})

    return {"status": "success", "message": "Pull request successfully merged into main!"}

@app.post("/api/v1/incidents/{incident_id}/reanalyze", tags=["Incidents"])
async def reanalyze_incident(
    incident_id: str,
    request: ReanalyzeRequest, # Pydantic model for input validation
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """Triggers a re-analysis of the incident using Gemini, passing engineering instructions."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database unavailable.")

    # 1. Verify Incident exists and is in 'fix_proposed' state
    incident = db["incidents"].find_one({"_id": incident_id})
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    
    # Optional: ensure status is appropriate for re-analysis
    if incident.get("status") not in ["fix_proposed", "analysis_failed"]:
         raise HTTPException(status_code=400, detail="Incident cannot be re-analyzed in current state.")

    # 2. Add optional 'instructions' to the original payload metadata
    original_payload = incident.get("raw_payload", {}) # Assuming you saved this!
    if "metadata" not in original_payload:
        original_payload["metadata"] = {}
    
    # Store engineering instructions in metadata only when supplied
    if request.instructions.strip():
        original_payload["metadata"]["engineering_instructions"] = request.instructions

    # 3. Fire the LLM re-analysis pipeline in the background
    background_tasks.add_task(run_ai_analysis_pipeline, incident_id, original_payload)

    return {"status": "accepted", "message": "Re-analysis triggered successfully!"}
# --- Generic Machine Ingest Webhook ---
@app.post("/api/v1/webhooks/ingest", tags=["Webhooks"])
async def handle_machine_ingest(
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    x_ingest_secret: str | None = Header(default=None),
    x_app_id: str | None = Header(default=None),
):
    """Generic machine-to-machine incident ingest endpoint secured by shared secret."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")
    ingest_context = authenticate_ingest_client(db, x_ingest_secret, x_app_id)
    logger.info(
        "called /api/v1/webhooks/ingest app_id=%s payload=%s",
        ingest_context.get("app_id"),
        payload,
    )

    if SUPPRESS_DEBUG_INCIDENTS and is_synthetic_debug_incident(payload) and not debug_suppression_bypassed(payload):
        logger.info("Ignoring synthetic debug incident from /api/erragent-debug")
        return {"status": "ignored_debug_event"}

    incident_id = ingest_machine_payload(
        db,
        background_tasks,
        payload,
        ingest_context["actor"],
        app_id=ingest_context.get("app_id"),
        app_default_repo=ingest_context.get("default_repo"),
    )
    return {"status": "accepted", "incident_id": incident_id}

# --- Vercel Webhook ---
@app.post("/api/v1/webhooks/vercel", tags=["Webhooks"])
async def handle_vercel_log_drain(logs: List[Dict[str, Any]], background_tasks: BackgroundTasks):
    """
    Ingests raw log streams directly from Vercel Log Drains.
    Zero app code modifications needed!
    """
    for log in logs:
        message = log.get("message", "")
        
        # Check if the log contains an unhandled exception or error
        if log.get("type") == "stderr" or "Traceback" in message or "ERROR:" in message:
            service_name = log.get("source", "vercel-app")
            
            payload = {
                "service_name": service_name,
                "environment": "production",
                "error_message": message.split("\n")[-1] if "\n" in message else message,
                "stack_trace": message,
                "repository": f"SummonShenron/{service_name}"
            }

            db = get_db()
            ingest_machine_payload(db, background_tasks, payload, "VERCEL_WEBHOOK")
            
    return {"status": "processed"}

# --- Render Webhook ---
@app.post("/api/v1/webhooks/render", tags=["Webhooks"])
async def handle_render_log_drain(payload: Dict[str, Any], background_tasks: BackgroundTasks):
    """
    Ingests raw log streams from Render Log Drains.
    """
    # Render sends log lines in payload["logs"] or payload["message"]
    log_text = payload.get("message", "") or str(payload)
    
    if "Traceback" in log_text or "ERROR" in log_text or "CRITICAL" in log_text:
        service_name = payload.get("service", {}).get("name", "render-fastapi-service")
        
        incident_data = {
            "service_name": service_name,
            "environment": "production",
            "error_message": log_text.split("\n")[-1] if "\n" in log_text else "Render Runtime Error",
            "stack_trace": log_text,
            "repository": f"SummonShenron/{service_name}"
        }

        db = get_db()
        ingest_machine_payload(db, background_tasks, incident_data, "RENDER_WEBHOOK")

    return {"status": "ok"}


# --- Sentry Webhook ---
@app.post("/api/v1/webhooks/sentry", tags=["Webhooks"])
async def handle_sentry_webhook(
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None),
):
    """Ingests Sentry webhook payloads using a shared secret rather than Clerk auth."""
    require_sentry_secret(x_webhook_secret)

    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")

    exception_values = ((payload.get("exception") or {}).get("values") or [{}])
    first_exception = exception_values[0] if exception_values else {}
    stack_frames = ((first_exception.get("stacktrace") or {}).get("frames") or [])
    rendered_stack = "\n".join(
        f"{frame.get('filename', 'unknown')}:{frame.get('lineno', '?')} in {frame.get('function', 'unknown')}"
        for frame in stack_frames[-10:]
    )

    incident_data = {
        "service_name": payload.get("project_name") or payload.get("logger") or "sentry-project",
        "environment": payload.get("environment", "production"),
        "error_message": first_exception.get("value") or payload.get("message") or payload.get("title") or "Sentry event",
        "stack_trace": rendered_stack or str(payload.get("exception")) or str(payload),
        "metadata": {
            "event_id": payload.get("event_id"),
            "level": payload.get("level"),
            "culprit": payload.get("culprit"),
            "url": payload.get("url"),
        },
    }

    incident_id = f"sentry_{payload.get('event_id') or int(datetime.now(timezone.utc).timestamp())}"
    ingest_machine_payload(
        db,
        background_tasks,
        incident_data,
        "SENTRY_WEBHOOK",
        incident_id=incident_id,
    )

    return {"status": "accepted", "incident_id": incident_id}