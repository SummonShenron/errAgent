from datetime import datetime, timezone
import logging
import os
from backend.utils.db_utils import get_db
logger = logging.getLogger("errAgent Logger")

DEFAULT_TARGET_REPO = os.getenv("DEFAULT_TARGET_REPO", "SummonShenron/SAAPP")

async def run_ai_analysis_pipeline(incident_id: str, payload: dict):
    db = get_db()
    if db is None:
        return

    # Update status to ANALYZING
    db["incidents"].update_one({"_id": incident_id}, {"$set": {"status": "analyzing"}})

    try:
        # -------------------------------------------------------------
        # TODO: Call your Llama / LangGraph Agent graph here!
        # Example agent execution output:
        # -------------------------------------------------------------
        root_cause = f"Exception caused by null payload in {payload.get('service_name')} execution flow."
        suggested_branch = f"fix/{incident_id}"
        
        # 1. Store Root Cause Analysis
        db["analyses"].insert_one({
            "incident_id": incident_id,
            "root_cause": root_cause,
            "summary": f"Detected unhandled exception in {payload.get('service_name')}.",
            "severity": "HIGH",
            "created_at": datetime.now(timezone.utc)
        })

        # 2. Store Draft Remediation Action for Human Approval
        db["remediations"].insert_one({
            "incident_id": incident_id,
            "action_type": "create_pull_request",
            "target_repo": payload.get("repository") or DEFAULT_TARGET_REPO,
            "base_branch": "main",
            "head_branch": suggested_branch,
            "pr_title": f"fix(autofix): resolve error in {payload.get('service_name')}",
            "pr_body": f"## AI Remediation Summary\n\n{root_cause}\n\n*Generated automatically by errAgent.*",
            "status": "pending_approval",
            "created_at": datetime.now(timezone.utc)
        })

        # Update status to FIX_PROPOSED
        db["incidents"].update_one({"_id": incident_id}, {"$set": {"status": "fix_proposed"}})

    except Exception as e:
        logger.error(f"Failed to execute AI analysis background task: {e}")
        db["incidents"].update_one({"_id": incident_id}, {"$set": {"status": "open"}})