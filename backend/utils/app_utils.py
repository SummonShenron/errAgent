from datetime import datetime, timezone
from pydantic import BaseModel, Field
import logging
import os
from backend.utils.db_utils import get_db
from backend.prompts.constraints import INCIDENT_ANALYSIS_PROMPT
from google import genai
from google.genai import types
import re

logger = logging.getLogger("errAgent Logger")

DEFAULT_TARGET_REPO = os.getenv("DEFAULT_TARGET_REPO", "SummonShenron/SAAPP")

class AIAnalysisSchema(BaseModel):
    root_cause_summary: str = Field(description="Concise 2-3 sentence root cause breakdown.")
    severity: str = Field(description="Severity rating: LOW, MEDIUM, HIGH, or CRITICAL.")
    suggested_fix: str = Field(description="Detailed explanation of the code fix.")
    code_patch: str = Field(
        description="Unified git diff or code snippet showing the exact before/after change, e.g.:\n"
                    "--- a/app.py\n+++ b/app.py\n@@ -1133,1 +1133,3 @@\n-return 1 / 0\n+if denominator == 0:\n+    return 0\n+return numerator / denominator"
    )
    head_branch: str = Field(description="Git branch name for the fix.")
    base_branch: str = Field(default="main", description="Target base branch.")
    pr_title: str = Field(description="Concise GitHub PR title.")
    pr_body: str = Field(description="Markdown PR body explaining the fix.")

def _extract_target_file_path(stack_trace: str) -> str:
    """Scans the stack trace for the user's source file, ignoring virtual environments."""
    if not stack_trace:
        return "app.py"
    
    lines = stack_trace.splitlines()
    for line in lines:
        if 'File "' in line:
            match = re.search(r'File "([^"]+)"', line)
            if match:
                filepath = match.group(1)
                # Skip python internals and site-packages to find actual app code
                if "site-packages" not in filepath and "venv" not in filepath and "lib/python" not in filepath:
                    return filepath.split("/")[-1]
                    
    return "app.py"

def run_ai_analysis_pipeline(incident_id: str, payload: dict) -> None:
    db = get_db()
    if db is None:
        logger.error("Database unavailable during AI analysis for %s", incident_id)
        return

    db["incidents"].update_one(
        {"_id": incident_id},
        {"$set": {"status": "analyzing", "updated_at": datetime.now(timezone.utc)}}
    )

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("GOOGLE_API_KEY is not configured.")
        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "analysis_failed", "updated_at": datetime.now(timezone.utc)}}
        )
        return

    client = genai.Client(api_key=api_key)

    # Extract target file path to pass into prompt and save into remediation
    stack_trace = payload.get("stack_trace", "No stack trace provided.")
    target_file = _extract_target_file_path(stack_trace)

    prompt = INCIDENT_ANALYSIS_PROMPT.format(
        service_name=payload.get("service_name", "unknown-service"),
        environment=payload.get("environment", "production"),
        stack_trace=stack_trace,
        git_diffs=payload.get("git_diffs", "No git diff context provided."),
        metadata=payload.get("metadata", {}),
        engineering_instructions=payload.get("engineering_instructions", ""),
        target_file_path=target_file
    )
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",  # Or your active model version
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIAnalysisSchema,
                temperature=0.1,
            ),
        )

        result: AIAnalysisSchema = response.parsed
        now = datetime.now(timezone.utc)

        # 1. Store AI Analysis Record
        db["analyses"].insert_one({
            "incident_id": incident_id,
            "root_cause_summary": result.root_cause_summary,
            "severity": result.severity,
            "suggested_fix": result.suggested_fix,
            "confidence_score": 0.95,
            "created_at": now,
        })

        # 2. Store Remediation PR Draft with target_file_path included!
        target_repo = payload.get("repository") or DEFAULT_TARGET_REPO
        db["remediations"].insert_one({
            "incident_id": incident_id,
            "status": "draft",
            "target_repo": target_repo,
            "code_patch": result.code_patch,
            "base_branch": result.base_branch,
            "head_branch": result.head_branch,
            "pr_title": result.pr_title,
            "pr_body": result.pr_body,
            "target_file_path": target_file, # <--- Saved here for frontend display
            "created_at": now,
            "updated_at": now,
        })

        # 3. Advance Incident State to 'fix_proposed'
        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "fix_proposed", "updated_at": now}}
        )

        logger.info("--> [errAgent AI] Successfully generated fix proposal for incident: %s", incident_id)

    except Exception as exc:
        logger.error("--> [errAgent AI] Analysis pipeline failed for %s: %s", incident_id, str(exc))
        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "analysis_failed", "updated_at": datetime.now(timezone.utc)}}
        )