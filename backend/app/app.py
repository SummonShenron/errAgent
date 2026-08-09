import os
import logging
from typing import List, Dict, Any
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import FastAPI, HTTPException, Depends, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
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


def _serialize_mongo_doc(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: _serialize_mongo_doc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_mongo_doc(item) for item in value]
    return value

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

    now = datetime.now(timezone.utc)
    incident_id = f"inc_{int(now.timestamp())}"

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
        "updated_at": now
    }

    db["incidents"].insert_one(incident_doc)

    db["audit_logs"].insert_one({
        "incident_id": incident_id,
        "actor": current_user.get("username", "INGESTION_WEBHOOK"),
        "action": "INCIDENT_CREATED",
        "details": {"service_name": incident_doc["service_name"]},
        "timestamp": now
    })

    # AUTOMATIC: Queue AI analysis & hotfix PR drafting
    background_tasks.add_task(run_ai_analysis_pipeline, incident_id, payload)

    return {"status": "created", "incident_id": incident_id}


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

    logger.info(f"Opening GitHub PR for {repo} ({head} -> {base})...")
    
    # Added 'await' here
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
            
            # Re-use your ingestion & Gemini analysis pipeline!
            now = datetime.now(timezone.utc)
            incident_id = f"inc_{int(now.timestamp())}"
            
            # Store in MongoDB and trigger background analysis
            db = get_db()
            db["incidents"].insert_one({
                "_id": incident_id,
                **payload,
                "status": "open",
                "created_at": now,
                "updated_at": now
            })
            
            background_tasks.add_task(run_ai_analysis_pipeline, incident_id, payload)
            
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
        
        now = datetime.now(timezone.utc)
        incident_id = f"inc_{int(now.timestamp())}"
        
        db = get_db()
        db["incidents"].insert_one({
            "_id": incident_id,
            **incident_data,
            "status": "open",
            "created_at": now,
            "updated_at": now
        })
        
        background_tasks.add_task(run_ai_analysis_pipeline, incident_id, incident_data)

    return {"status": "ok"}