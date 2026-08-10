import os
import hmac
import logging
from typing import List, Dict, Any
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import FastAPI, HTTPException, Depends, status, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from backend.schemas.incident_schemas import IncidentCreate, IncidentInDB, IncidentStatus, AuditLogEntry
# Utility imports from your backend/utils directory
from backend.utils.db_utils import get_db
from backend.utils.app_utils import run_ai_analysis_pipeline
from backend.utils.isolation_auth import get_current_user
from backend.services.github_service import GitHubOpsService
from backend.middleware.rbac import require_role

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ErrAgent Logger")

app = FastAPI(title="errAgent Incident Engine", version="1.0.0")

github_service = GitHubOpsService()
SENTRY_WEBHOOK_SECRET = os.getenv("SENTRY_WEBHOOK_SECRET")
# Backward-compatible legacy shared secret for existing app-to-app clients.
INGEST_WEBHOOK_SECRET = os.getenv("INGEST_WEBHOOK_SECRET") or os.getenv("ERRAGENT_INGEST_SECRET")
DEFAULT_TARGET_REPO = os.getenv("DEFAULT_TARGET_REPO", "SummonShenron/SAAPP")

class ReanalyzeRequest(BaseModel):
    instructions: str

def _serialize_mongo_doc(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: _serialize_mongo_doc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_mongo_doc(item) for item in value]
    return value


def _store_incident_and_queue_analysis(
    db,
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any],
    actor: str,
    incident_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    incident_id = incident_id or f"inc_{int(now.timestamp())}"

    incident_doc = {
        "_id": incident_id,
        "service_name": payload.get("service_name", "unknown-service"),
        "environment": payload.get("environment", "production"),
        "error_message": payload.get("error_message", "Unhandled Exception"),
        "stack_trace": payload.get("stack_trace", ""),
        "repository": payload.get("repository", ""),
        "status": "open",
        "metadata": payload.get("metadata", {}),
        "created_at": now,
        "updated_at": now,
    }

    db["incidents"].insert_one(incident_doc)
    db["audit_logs"].insert_one({
        "incident_id": incident_id,
        "actor": actor,
        "action": "INCIDENT_CREATED",
        "details": {"service_name": incident_doc["service_name"]},
        "timestamp": now,
    })
    background_tasks.add_task(run_ai_analysis_pipeline, incident_id, payload)
    return incident_id


def _require_sentry_secret(incoming_secret: str | None):
    if not SENTRY_WEBHOOK_SECRET:
        logger.error("SENTRY_WEBHOOK_SECRET is not configured.")
        raise HTTPException(status_code=500, detail="Webhook configuration error")
    if incoming_secret != SENTRY_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


def _authenticate_ingest_client(db, incoming_secret: str | None, app_id: str | None) -> Dict[str, Any]:
    """
    Returns ingestion context:
    {
      "actor": str,
      "app_id": str | None,
      "default_repo": str | None,
    }

    Behavior:
    - If x-app-id is provided, validate against ingest_clients collection.
    - If no x-app-id, fallback to legacy shared secret for backward compatibility.
    """
    if not incoming_secret:
        raise HTTPException(status_code=401, detail="Missing ingest secret")

    if app_id:
        client = db["ingest_clients"].find_one({"app_id": app_id, "enabled": True}) or {}
        expected_secret = client.get("secret")
        if not expected_secret or not hmac.compare_digest(str(incoming_secret), str(expected_secret)):
            raise HTTPException(status_code=401, detail="Invalid ingest credentials")

        return {
            "actor": f"MACHINE_INGEST:{app_id}",
            "app_id": app_id,
            "default_repo": client.get("default_repo"),
        }

    if not INGEST_WEBHOOK_SECRET:
        logger.error("INGEST_WEBHOOK_SECRET (or ERRAGENT_INGEST_SECRET) is not configured.")
        raise HTTPException(status_code=500, detail="Ingest configuration error")

    if not hmac.compare_digest(str(incoming_secret), str(INGEST_WEBHOOK_SECRET)):
        raise HTTPException(status_code=401, detail="Invalid ingest secret")

    return {
        "actor": "MACHINE_INGEST",
        "app_id": None,
        "default_repo": None,
    }


def _extract_repository_from_tag_collection(tag_collection: Any) -> str | None:
    """Supports Sentry tags as dict, list[{key,value}], or list[[key,value]]."""
    if isinstance(tag_collection, dict):
        for key in ("repository", "repo", "target_repo", "github_repo"):
            value = tag_collection.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    if isinstance(tag_collection, list):
        for item in tag_collection:
            if isinstance(item, dict):
                key = str(item.get("key", "")).strip().lower()
                value = item.get("value")
                if key in {"repository", "repo", "target_repo", "github_repo"} and isinstance(value, str) and value.strip():
                    return value.strip()
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                key = str(item[0]).strip().lower()
                value = item[1]
                if key in {"repository", "repo", "target_repo", "github_repo"} and isinstance(value, str) and value.strip():
                    return value.strip()

    return None


def _lookup_repo_from_service_registry(db, service_name: str, app_id: str | None = None) -> str | None:
    if not service_name:
        return None

    # Prefer app-specific service mapping when app_id is present.
    registry_entry = {}
    if app_id:
        registry_entry = db["service_registry"].find_one({"service_name": service_name, "app_id": app_id}) or {}

    if not registry_entry:
        registry_entry = db["service_registry"].find_one({"service_name": service_name}) or {}

    for key in ("target_repo", "repository", "repo"):
        value = registry_entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_target_repository(
    db,
    payload: Dict[str, Any],
    app_id: str | None = None,
    app_default_repo: str | None = None,
) -> str:
    direct_repo = payload.get("repository")
    if isinstance(direct_repo, str) and direct_repo.strip():
        return direct_repo.strip()

    for collection in (
        payload.get("tags"),
        (payload.get("data") or {}).get("tags"),
    ):
        resolved = _extract_repository_from_tag_collection(collection)
        if resolved:
            return resolved

    extra_repo = (payload.get("extra") or {}).get("repository")
    if isinstance(extra_repo, str) and extra_repo.strip():
        return extra_repo.strip()

    service_name = payload.get("service_name") or payload.get("project_name") or payload.get("logger")
    if isinstance(service_name, str):
        resolved_from_registry = _lookup_repo_from_service_registry(db, service_name.strip(), app_id=app_id)
        if resolved_from_registry:
            return resolved_from_registry

    if isinstance(app_default_repo, str) and app_default_repo.strip():
        return app_default_repo.strip()

    return DEFAULT_TARGET_REPO


def _ingest_machine_payload(
    db,
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any],
    actor: str,
    incident_id: str | None = None,
    app_id: str | None = None,
    app_default_repo: str | None = None,
) -> str:
    normalized_payload = dict(payload)
    normalized_payload["service_name"] = (
        normalized_payload.get("service_name")
        or normalized_payload.get("project_name")
        or normalized_payload.get("logger")
        or "unknown-service"
    )
    normalized_payload["repository"] = _resolve_target_repository(
        db,
        normalized_payload,
        app_id=app_id,
        app_default_repo=app_default_repo,
    )
    if app_id:
        metadata = normalized_payload.get("metadata") or {}
        if isinstance(metadata, dict):
            metadata["app_id"] = app_id
            normalized_payload["metadata"] = metadata
    return _store_incident_and_queue_analysis(
        db,
        background_tasks,
        normalized_payload,
        actor,
        incident_id=incident_id,
    )

# Enable CORS for Frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 1. HEALTH CHECK ENDPOINT ---
@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok", "service": "ErrAgent Backend Engine"}


# --- 2. LIST ALL INCIDENTS ---
@app.get("/api/v1/incidents", response_model=List[Dict[str, Any]], tags=["Incidents"])
async def list_incidents(current_user: dict = Depends(get_current_user)):
    """Fetches all incidents from MongoDB, ordered by most recent."""
    db = get_db()
    if db is None:
        raise HTTPException(status_code=500, detail="Database connection unavailable.")
    
    incidents = list(db["incidents"].find({}).sort("created_at", -1))
    return _serialize_mongo_doc(incidents)


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

    analysis = db["analyses"].find_one({"incident_id": incident_id}) or {}
    remediation = db["remediations"].find_one({"incident_id": incident_id}) or {}

    return {
        "incident": _serialize_mongo_doc(incident),
        "analysis": _serialize_mongo_doc(analysis),
        "remediation": _serialize_mongo_doc(remediation)
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
    incident_id = _ingest_machine_payload(
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
    remediation = db["remediations"].find_one({"incident_id": incident_id})
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
    
    # Extract target file and code patch stored in remediation document
    file_path = remediation.get("target_file_path", "main.py") 
    file_content = remediation.get("code_patch", "")

    logger.info(f"Creating branch {head} and pushing commit for {repo}...")
    
    # Step A: Create the branch and commit the code fix first
    await github_service.create_branch_and_commit(
        repo=repo,
        base_branch=base,
        new_branch=head,
        file_path=file_path,
        file_content=file_content,
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
        {"$set": {"status": "executed", "approved_by": actor_username, "pr_url": pr_url, "updated_at": now}}
    )
    
    db["incidents"].update_one(
        {"_id": incident_id},
        {"$set": {"status": "resolved", "updated_at": now}}
    )

    # 4. Audit Log
    db["audit_logs"].insert_one({
        "incident_id": incident_id,
        "actor": actor_username,
        "action": "HOTFIX_APPROVED_AND_EXECUTED",
        "details": {"pr_url": pr_url},
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
    remediation = db["remediations"].find_one({"incident_id": incident_id})
    if not remediation:
        raise HTTPException(status_code=404, detail="No remediation draft found.")

    repo = remediation["target_repo"]
    title = remediation["pr_title"]
    body = remediation["pr_body"]
    head = remediation["head_branch"]
    base = remediation["base_branch"]
    file_path = remediation.get("target_file_path", "main.py")
    file_content = remediation.get("code_patch", "")

    # 1. Push branch & commit
    await github_service.create_branch_and_commit(
        repo=repo, base_branch=base, new_branch=head, 
        file_path=file_path, file_content=file_content, commit_message=f"Fix incident: {title}"
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

    # Update state: PR is created, waiting for human merge decision
    db["remediations"].update_one(
        {"incident_id": incident_id},
        {"$set": {
            "status": "pr_created", 
            "pr_url": pr_url, 
            "pr_number": pr_number, 
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
    remediation = db["remediations"].find_one({"incident_id": incident_id})
    if not remediation or not remediation.get("pr_number"):
        raise HTTPException(status_code=404, detail="No active PR found to merge.")

    repo = remediation["target_repo"]
    pr_number = remediation["pr_number"]

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

    # 2. Add 'instructions' to the original payload metadata
    original_payload = incident.get("raw_payload", {}) # Assuming you saved this!
    if "metadata" not in original_payload:
        original_payload["metadata"] = {}
    
    # Store engineering instructions in metadata
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
    ingest_context = _authenticate_ingest_client(db, x_ingest_secret, x_app_id)
    logger.info(
        "called /api/v1/webhooks/ingest app_id=%s payload=%s",
        ingest_context.get("app_id"),
        payload,
    )
    incident_id = _ingest_machine_payload(
        db,
        background_tasks,
        payload,
        ingest_context["actor"],
        app_id=ingest_context.get("app_id"),
        app_default_repo=ingest_context.get("default_repo"),
    )
    background_tasks.add_task(run_ai_analysis_pipeline, incident_id, payload)
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
            _ingest_machine_payload(db, background_tasks, payload, "VERCEL_WEBHOOK")
            
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
        _ingest_machine_payload(db, background_tasks, incident_data, "RENDER_WEBHOOK")

    return {"status": "ok"}


# --- Sentry Webhook ---
@app.post("/api/v1/webhooks/sentry", tags=["Webhooks"])
async def handle_sentry_webhook(
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None),
):
    """Ingests Sentry webhook payloads using a shared secret rather than Clerk auth."""
    _require_sentry_secret(x_webhook_secret)

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
    _ingest_machine_payload(
        db,
        background_tasks,
        incident_data,
        "SENTRY_WEBHOOK",
        incident_id=incident_id,
    )

    return {"status": "accepted", "incident_id": incident_id}