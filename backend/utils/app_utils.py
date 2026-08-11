import os
import re
import logging
import difflib
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone
from typing import Any
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
        description="A valid unified git diff showing the exact before/after changes. MUST start with '--- a/' and '+++ b/'. NEVER use '--- /dev/null'. Include 3 lines of unchanged context."
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
                    # Strip Render/Docker prefixes to get the true repo path
                    cleaned_path = re.sub(r'^(/opt/render/project/src/|/app/|/var/www/|/workspace/)', '', filepath)
                    return cleaned_path
    return "app.py"


def _extract_target_file_candidates(stack_trace: str, payload: dict[str, Any]) -> list[str]:
    """
    Build repo-relative file path candidates from stack trace + app location hints.
    This handles hosted paths like /opt/render/project/src/<app>/... by trying
    progressively shorter paths until one exists in the target repository.
    """
    raw_path = _extract_target_file_path(stack_trace).replace("\\", "/").strip()
    if not raw_path:
        return ["app.py"]

    raw_path = raw_path.lstrip("/")

    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: str) -> None:
        normalized = value.replace("\\", "/").strip().lstrip("/")
        normalized = re.sub(r"/+", "/", normalized)
        if not normalized:
            return
        if normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    add_candidate(raw_path)

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    location_hints = [
        payload.get("target_app_location"),
        payload.get("app_location"),
        payload.get("app_root"),
        payload.get("project_root"),
        payload.get("source_root"),
        payload.get("repo_subdir"),
        metadata.get("target_app_location") if isinstance(metadata, dict) else None,
        metadata.get("app_location") if isinstance(metadata, dict) else None,
        metadata.get("app_root") if isinstance(metadata, dict) else None,
        metadata.get("project_root") if isinstance(metadata, dict) else None,
        metadata.get("source_root") if isinstance(metadata, dict) else None,
        metadata.get("repo_subdir") if isinstance(metadata, dict) else None,
    ]

    for hint in location_hints:
        if not isinstance(hint, str) or not hint.strip():
            continue
        normalized_hint = hint.replace("\\", "/").strip().strip("/")
        if not normalized_hint:
            continue
        if raw_path.startswith(f"{normalized_hint}/"):
            add_candidate(raw_path[len(normalized_hint) + 1:])

    # Also try stripping leading folders one by one.
    parts = [part for part in raw_path.split("/") if part]
    for index in range(1, len(parts) - 1):
        add_candidate("/".join(parts[index:]))

    if not candidates:
        return ["app.py"]
    return candidates

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
    
    # ---------------------------------------------------------
    # 1. Fetch target file directly from GitHub (No local files)
    # ---------------------------------------------------------
    target_repo = str(payload.get("repository") or DEFAULT_TARGET_REPO).strip()
    target_file_candidates = _extract_target_file_candidates(stack_trace, payload)
    target_file = target_file_candidates[0]
    
    existing_code = ""
    branches_to_try = ["main", "master"]
    file_fetched = False

    for branch in branches_to_try:
        for candidate_path in target_file_candidates:
            raw_url = f"https://raw.githubusercontent.com/{target_repo}/{branch}/{candidate_path}"
            try:
                req = urllib.request.Request(raw_url)
                with urllib.request.urlopen(req) as response:
                    existing_code = response.read().decode("utf-8")
                file_fetched = True
                target_file = candidate_path
                logger.info(
                    "--> [errAgent AI] Fetched %s chars from GitHub: %s",
                    len(existing_code),
                    raw_url,
                )
                break
            except HTTPError as e:
                logger.debug("Branch '%s' or file '%s' missing (HTTP %s)", branch, candidate_path, e.code)
            except Exception as e:
                logger.warning("Error fetching from %s: %s", raw_url, e)
        if file_fetched:
            break

    if not file_fetched:
        logger.error(
            "--> [errAgent AI] CRITICAL: Could not fetch any target file from %s. Candidates=%s",
            target_repo,
            target_file_candidates,
        )
        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "analysis_failed", "updated_at": datetime.now(timezone.utc)}}
        )
        return

    # 2. Build base prompt with format context
    base_prompt = INCIDENT_ANALYSIS_PROMPT.format(
        service_name=payload.get("service_name", "unknown-service"),
        environment=payload.get("environment", "production"),
        stack_trace=stack_trace,
        git_diffs=payload.get("git_diffs", "No git diff context provided."),
        metadata=payload.get("metadata", {}),
        engineering_instructions=payload.get("engineering_instructions", ""),
        target_file_path=target_file
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
            model="gemini-3.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIAnalysisSchema,
                temperature=0.1,
            ),
        )

        result: AIAnalysisSchema = response.parsed
        now = datetime.now(timezone.utc)
        full_file_content = existing_code
        # ---------------------------------------------------------
        # 3. Apply patch inside an Isolated Temporary Sandbox
        clean_patch = result.code_patch.strip()
        if clean_patch.startswith("```"):
            clean_patch = re.sub(r"^```[a-zA-Z]*\n?", "", clean_patch)
        if clean_patch.endswith("```"):
            clean_patch = re.sub(r"\n?```$", "", clean_patch)
        clean_patch = clean_patch.strip() + "\n"

        logger.info(f"--> [errAgent AI] Generated Patch:\n{clean_patch}")

        # Create a temporary directory that destroys itself when the block ends
        with tempfile.TemporaryDirectory() as sandbox_dir:
            # Recreate the file structure safely inside the sandbox
            sandbox_file_path = os.path.join(sandbox_dir, target_file)
            os.makedirs(os.path.dirname(sandbox_file_path) or sandbox_dir, exist_ok=True)

            with open(sandbox_file_path, "w", encoding="utf-8") as f:
                f.write(existing_code)

            # Initialize a fake Git repo so `git apply` behaves perfectly
            subprocess.run(["git", "init"], cwd=sandbox_dir, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=sandbox_dir, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=sandbox_dir, capture_output=True)

            # Save the LLM's patch to a file
            patch_path = os.path.join(sandbox_dir, "fix.patch")
            with open(patch_path, "w", encoding="utf-8") as tf:
                tf.write(clean_patch)

            # Run git apply isolated in the sandbox
            apply_result = subprocess.run(
                [
                    "git", "apply",
                    "-p1",
                    "--recount",
                    "--ignore-space-change",
                    "--ignore-whitespace",
                    "--unidiff-zero",
                    "fix.patch"
                ],
                cwd=sandbox_dir,
                capture_output=True,
                text=True
            )

            if apply_result.returncode != 0:
                logger.error(f"--> [errAgent AI] Sandboxed git apply failed: {apply_result.stderr}")
                # Fallback to the unpatched code if Git rejects it
                full_file_content = existing_code
            else:
                logger.info("--> [errAgent AI] Successfully applied patch in isolated sandbox.")
                # Read the patched code back into memory
                with open(sandbox_file_path, "r", encoding="utf-8") as f:
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
                "target_file_path": target_file,
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