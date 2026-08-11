import os
import re
import logging
import difflib
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from backend.utils.db_utils import get_db
from backend.prompts.constraints import INCIDENT_ANALYSIS_PROMPT
from google import genai
from google.genai import types
import subprocess
import tempfile

logger = logging.getLogger("errAgent Logger")
DEFAULT_TARGET_REPO = os.getenv("DEFAULT_TARGET_REPO", "SummonShenron/SAAPP")

class AIAnalysisSchema(BaseModel):
    root_cause_summary: str = Field(description="Concise 2-3 sentence root cause breakdown.")
    severity: str = Field(description="Severity rating: LOW, MEDIUM, HIGH, or CRITICAL.")
    suggested_fix: str = Field(description="Detailed explanation of the code fix.")
    code_patch: str = Field(
        description="A valid unified git diff showing the exact before/after change for the target file."
    )
    head_branch: str = Field(description="Git branch name for the fix.")
    base_branch: str = Field(default="main", description="Target base branch.")
    pr_title: str = Field(description="Concise GitHub PR title.")
    pr_body: str = Field(description="Markdown PR body explaining the fix.")

def _extract_target_file_path(stack_trace: str) -> str:
    if not stack_trace:
        return "app.py"
    lines = stack_trace.splitlines()
    for line in lines:
        if 'File "' in line:
            match = re.search(r'File "([^"]+)"', line)
            if match:
                filepath = match.group(1)
                if "site-packages" not in filepath and "venv" not in filepath and "lib/python" not in filepath:
                    # Strip common server prefixes but KEEP the subdirectories
                    # e.g., /opt/render/project/src/backend/api/app.py -> backend/api/app.py
                    cleaned_path = re.sub(r'^(/opt/render/project/src/|/app/|/var/www/)', '', filepath)
                    return cleaned_path
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
    stack_trace = payload.get("stack_trace", "No stack trace provided.")
    target_file = _extract_target_file_path(stack_trace)

    # 1. Locate the file on Render (/opt/render/project/src/app.py) or locally
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(current_dir))

    possible_paths = [
        os.path.join(project_root, target_file),
        os.path.join(os.getcwd(), target_file),
        target_file
    ]

    existing_code = "# File does not exist yet or is empty"
    actual_target_file = target_file
    git_root = os.getcwd() # Default fallback

    for path in possible_paths:
        if os.path.exists(path):
            actual_target_file = path
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing_code = f.read()
                logger.info(f"--> [errAgent AI] Loaded {len(existing_code)} chars from: {path}")
                
                # NEW: Find the root of the git repository for THIS specific app
                try:
                    git_root = subprocess.check_output(
                        ["git", "rev-parse", "--show-toplevel"], 
                        cwd=os.path.dirname(actual_target_file), text=True
                    ).strip()
                except Exception:
                    git_root = os.path.dirname(actual_target_file)
                
                logger.info(f"--> [errAgent AI] Identified Git Root for app: {git_root}")
                break
            except Exception as e:
                logger.warning(f"Could not read {path}: {e}")

    # 2. Build base prompt with format context
    base_prompt = INCIDENT_ANALYSIS_PROMPT.format(
        service_name=payload.get("service_name", "unknown-service"),
        environment=payload.get("environment", "production"),
        stack_trace=stack_trace,
        git_diffs=payload.get("git_diffs", "No git diff context provided."),
        metadata=payload.get("metadata", {}),
        engineering_instructions=payload.get("engineering_instructions", ""),
        target_file_path=actual_target_file
    )

    # Append file contents safely via f-string
    prompt = f"""{base_prompt}
--------------------------------------------------
CURRENT CONTENT OF TARGET FILE ({target_file}):
```python
{existing_code}
```
    """
    logger.info(f"--> [errAgent AI] Prompt sent to Gemini (first 500 chars):\n{prompt[:500]}...")
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash",  # Using gemini-3.5-flash
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIAnalysisSchema,
                temperature=0.1,
            ),
        )

        result: AIAnalysisSchema = response.parsed
        now = datetime.now(timezone.utc)

        # 3. Sanitize markdown fences from patch before applying
        full_file_content = ""
        try:
            clean_patch = result.code_patch.strip()
            if clean_patch.startswith("```"):
                clean_patch = re.sub(r"^```[a-zA-Z]*\n?", "", clean_patch)
            if clean_patch.endswith("```"):
                clean_patch = re.sub(r"\n?```$", "", clean_patch)
            clean_patch = clean_patch.strip() + "\n"

            logger.info(f"--> [errAgent AI] Generated Patch:\n{clean_patch}")

            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.patch', encoding='utf-8') as tf:
                tf.write(clean_patch)
                tf_name = tf.name

            # Run git apply from the GIT ROOT of the target app.
            # -p1 handles the "a/" and "b/" prefixes standard in multi-file git diffs.
            apply_result = subprocess.run(
                [
                    "git", "apply", 
                    "-p1",                   # Maps standard unified diff paths across multiple files
                    "--recount",             
                    "--ignore-space-change", 
                    "--ignore-whitespace",   
                    "--unidiff-zero",        
                    tf_name
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=git_root  # <--- CRITICAL: Runs in the root of the repo being patched
            )
            os.unlink(tf_name)

            if apply_result.returncode != 0:
                logger.error(f"git apply failed: {apply_result.stderr}")
                subprocess.run(["git", "checkout", actual_target_file], capture_output=True, cwd=git_root)
            else:
                logger.info(f"--> [errAgent AI] Successfully applied patch from git root: {git_root}")

            if os.path.exists(actual_target_file):
                with open(actual_target_file, "r", encoding="utf-8") as f:
                    full_file_content = f.read()

        except Exception as patch_err:
            logger.error(f"Error applying patch: {patch_err}")
            if os.path.exists(actual_target_file):
                with open(actual_target_file, "r", encoding="utf-8") as f:
                    full_file_content = f.read()

        # 4. Save to Database
        db["analyses"].insert_one({
            "incident_id": incident_id,
            "root_cause_summary": result.root_cause_summary,
            "severity": result.severity,
            "suggested_fix": result.suggested_fix,
            "confidence_score": 0.95,
            "created_at": now,
        })

        target_repo = payload.get("repository") or DEFAULT_TARGET_REPO
        db["remediations"].insert_one({
            "incident_id": incident_id,
            "status": "draft",
            "target_repo": target_repo,
            "code_patch": result.code_patch,
            "full_file_content": full_file_content,
            "base_branch": result.base_branch,
            "head_branch": result.head_branch,
            "pr_title": result.pr_title,
            "pr_body": result.pr_body,
            "target_file_path": actual_target_file,
            "created_at": now,
            "updated_at": now,
        })

        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "fix_proposed", "updated_at": now}}
        )

        logger.info("--> [errAgent AI] Successfully applied patch and generated fix for incident: %s", incident_id)

    except Exception as exc:
        logger.error("--> [errAgent AI] Analysis pipeline failed for %s: %s", incident_id, str(exc))
        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "analysis_failed", "updated_at": datetime.now(timezone.utc)}}
        )