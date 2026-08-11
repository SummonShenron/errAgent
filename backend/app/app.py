import os
import hmac
import json
import hashlib
import logging
from uuid import uuid4
from typing import List, Dict, Any
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from fastapi import FastAPI, HTTPException, Depends, status, BackgroundTasks, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError
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
SUPPRESS_DEBUG_INCIDENTS = os.getenv("SUPPRESS_DEBUG_INCIDENTS", "false").lower() in {"1", "true", "yes", "on"}
INCIDENT_DEDUPE_WINDOW_SECONDS = int(os.getenv("INCIDENT_DEDUPE_WINDOW_SECONDS", "600"))

class ReanalyzeRequest(BaseModel):
    instructions: str = ""

def _serialize_mongo_doc(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: _serialize_mongo_doc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialize_mongo_doc(item) for item in value]
    return value


def _resolve_commit_file_content(remediation: Dict[str, Any]) -> str:
    """
    Commit payload must be the full file content, never a unified diff string.
    """
    file_content = remediation.get("full_file_content")
    if not isinstance(file_content, str) or not file_content.strip():
        raise HTTPException(
            status_code=409,
            detail=(
                "Remediation is missing full_file_content. Re-analyze incident before approving hotfix."
            ),
        )

    content_source = remediation.get("content_source")
    if content_source != "sandbox_applied":
        raise HTTPException(
            status_code=409,
            detail=(
                "Remediation content source is not trusted for commit. "
                "Re-analyze incident before approving hotfix."
            ),
        )

    stripped = file_content.lstrip()
    if stripped.startswith("diff --git ") or stripped.startswith("--- a/") or stripped.startswith("+++ b/"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Remediation full_file_content looks like patch text, not file contents. "
                "Re-analyze incident before approving hotfix."
            ),
        )

    actual_hash = hashlib.sha256(file_content.encode("utf-8")).hexdigest()
    expected_hash = remediation.get("full_file_content_sha256")
    if not isinstance(expected_hash, str) or not expected_hash.strip():
        raise HTTPException(
            status_code=409,
            detail=(
                "Remediation is missing full_file_content_sha256. "
                "Re-analyze incident before approving hotfix."
            ),
        )
    if isinstance(expected_hash, str) and expected_hash.strip() and expected_hash != actual_hash:
        raise HTTPException(
            status_code=409,
            detail=(
                "Remediation content hash mismatch detected. Refusing to commit potentially stale "
                "or corrupted file content. Re-analyze incident before approving hotfix."
            ),
        )

    return file_content


def _build_incident_fingerprint(payload: Dict[str, Any]) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    canonical = {
        "service_name": str(payload.get("service_name") or "").strip().lower(),
        "environment": str(payload.get("environment") or "").strip().lower(),
        "repository": str(payload.get("repository") or "").strip().lower(),
        "error_message": str(payload.get("error_message") or "").strip(),
        "stack_trace": str(payload.get("stack_trace") or "").strip(),
        "source": str(metadata.get("source") or "").strip(),
        "exception_type": str(metadata.get("exception_type") or "").strip(),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _is_synthetic_debug_incident(payload: Dict[str, Any]) -> bool:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    source = str(metadata.get("source") or "")
    stack_trace = str(payload.get("stack_trace") or "")
    return source == "/api/erragent-debug" or "/api/erragent-debug" in stack_trace


def _debug_suppression_bypassed(payload: Dict[str, Any]) -> bool:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return bool(payload.get("force_ingest_debug") or metadata.get("force_ingest_debug"))


def _store_incident_and_queue_analysis(
    db,
    background_tasks: BackgroundTasks,
    payload: Dict[str, Any],
    actor: str,
    incident_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    incident_id = incident_id or f"inc_{int(now.timestamp() * 1000)}_{uuid4().hex[:8]}"
    fingerprint = _build_incident_fingerprint(payload)

    dedupe_cutoff = now - timedelta(seconds=max(30, INCIDENT_DEDUPE_WINDOW_SECONDS))
    existing = db["incidents"].find_one(
        {
            "fingerprint": fingerprint,
            "created_at": {"$gte": dedupe_cutoff},
            "status": {"$in": ["open", "analyzing", "fix_proposed"]},
        },
        sort=[("created_at", -1)],
    )
    if existing:
        existing_id = existing.get("_id")
        logger.info("Skipping duplicate incident ingest. fingerprint=%s existing_id=%s", fingerprint, existing_id)
        return str(existing_id)

    incident_doc = {
        "_id": incident_id,
        "service_name": payload.get("service_name", "unknown-service"),
        "environment": payload.get("environment", "production"),
        "error_message": payload.get("error_message", "Unhandled Exception"),
        "stack_trace": payload.get("stack_trace", ""),
        "repository": payload.get("repository", ""),
        "fingerprint": fingerprint,
        "status": "open",
        "metadata": payload.get("metadata", {}),
        "created_at": now,
        "updated_at": now,
    }

    try:
        db["incidents"].insert_one(incident_doc)
    except DuplicateKeyError:
        existing_after_race = db["incidents"].find_one(
            {
                "$or": [
                    {"_id": incident_id},
                    {"fingerprint": fingerprint},
                ]
            },
            sort=[("created_at", -1)],
        )
        if existing_after_race:
            existing_id = existing_after_race.get("_id")
            logger.info(
                "Duplicate incident insert raced with another request. fingerprint=%s existing_id=%s",
                fingerprint,
                existing_id,
            )
            return str(existing_id)
        raise

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

    analysis = db["analyses"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}

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
    file_content = _resolve_commit_file_content(remediation)
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
        {"$set": {"status": "resolved", "updated_at": now}}
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
    file_content = _resolve_commit_file_content(remediation)
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
    ingest_context = _authenticate_ingest_client(db, x_ingest_secret, x_app_id)
    logger.info(
        "called /api/v1/webhooks/ingest app_id=%s payload=%s",
        ingest_context.get("app_id"),
        payload,
    )

    if SUPPRESS_DEBUG_INCIDENTS and _is_synthetic_debug_incident(payload) and not _debug_suppression_bypassed(payload):
        logger.info("Ignoring synthetic debug incident from /api/erragent-debug")
        return {"status": "ignored_debug_event"}

    incident_id = _ingest_machine_payload(
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