import os
import re
import hashlib
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


def _upsert_remediation_failure(
    db,
    incident_id: str,
    reason: str,
    target_repo: str | None = None,
    target_file: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    update_doc = {
        "status": "analysis_failed",
        "failure_reason": str(reason),
        "updated_at": now,
    }
    if target_repo:
        update_doc["target_repo"] = target_repo
    if target_file:
        update_doc["target_file_path"] = target_file

    db["remediations"].update_one(
        {"incident_id": incident_id},
        {
            "$set": update_doc,
            "$setOnInsert": {
                "incident_id": incident_id,
                "created_at": now,
            },
        },
        upsert=True,
    )


def _build_unique_head_branch(base_branch_name: str, incident_id: str) -> str:
    """Ensure each remediation uses a unique branch to avoid stale-branch collisions."""
    candidate = (base_branch_name or "fix/auto-remediation").strip().lower()
    candidate = re.sub(r"[^a-z0-9/_-]", "-", candidate)
    candidate = re.sub(r"-+", "-", candidate).strip("-/")
    if not candidate:
        candidate = "fix/auto-remediation"
    suffix = re.sub(r"[^a-zA-Z0-9_-]", "", incident_id)[-12:] or "incident"
    return f"{candidate}-{suffix}"

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


def _validate_patch_safety(clean_patch: str, target_file: str, existing_code: str) -> None:
    """
    Reject malformed or destructive patches before `git apply`.
    Guardrails:
    - Patch must be a diff (no prose/code prelude before diff headers).
    - Patch must modify exactly one file and that file must be target_file.
    - Patch must not look like a near-total file deletion.
    """
    if not clean_patch.strip():
        raise ValueError("Patch rejected: empty patch content.")

    lines = clean_patch.splitlines()
    first_non_empty = next((line.strip() for line in lines if line.strip()), "")
    if not (first_non_empty.startswith("--- a/") or first_non_empty.startswith("diff --git ")):
        raise ValueError("Patch rejected: patch must start with a unified diff header.")

    if "--- a/" not in clean_patch or "+++ b/" not in clean_patch:
        raise ValueError("Patch rejected: missing required unified diff file headers.")

    normalized_target = target_file.replace("\\", "/").lstrip("/")
    old_paths = re.findall(r"^--- a/(.+)$", clean_patch, flags=re.MULTILINE)
    new_paths = re.findall(r"^\+\+\+ b/(.+)$", clean_patch, flags=re.MULTILINE)

    if not old_paths or not new_paths:
        raise ValueError("Patch rejected: could not parse file headers.")

    touched_old = {path.strip() for path in old_paths}
    touched_new = {path.strip() for path in new_paths}
    touched = touched_old | touched_new

    if len(touched) != 1:
        raise ValueError(f"Patch rejected: patch must touch exactly one file. Found={sorted(touched)}")

    only_touched = next(iter(touched))
    if only_touched != normalized_target:
        raise ValueError(
            f"Patch rejected: patch targets '{only_touched}' but expected '{normalized_target}'."
        )

    # Reject suspicious near-total deletion patterns.
    deleted_lines = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    added_lines = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    original_line_count = max(1, len(existing_code.splitlines()))

    if deleted_lines > int(original_line_count * 0.8):
        raise ValueError(
            "Patch rejected: deletion volume is too high relative to target file size."
        )

    # Reject near-total rewrites that start from the top of the file.
    first_hunk = re.search(r"^@@\s*-([0-9]+)(?:,[0-9]+)?\s+\+([0-9]+)(?:,[0-9]+)?\s+@@", clean_patch, flags=re.MULTILINE)
    if first_hunk:
        old_start = int(first_hunk.group(1))
        new_start = int(first_hunk.group(2))
        if old_start <= 3 and new_start <= 3 and deleted_lines > int(original_line_count * 0.5):
            raise ValueError(
                "Patch rejected: looks like a full-file rewrite diff. Provide minimal localized hunks only."
            )

    if deleted_lines > int(original_line_count * 0.6) and added_lines < int(deleted_lines * 0.2):
        raise ValueError(
            "Patch rejected: patch appears to remove most of the file without equivalent replacement."
        )


def _normalize_generated_patch(raw_patch: str) -> str:
    clean_patch = (raw_patch or "").strip()
    if clean_patch.startswith("```"):
        clean_patch = re.sub(r"^```[a-zA-Z]*\n?", "", clean_patch)
    if clean_patch.endswith("```"):
        clean_patch = re.sub(r"\n?```$", "", clean_patch)
    return clean_patch.strip() + "\n"


def _apply_patch_in_sandbox(target_file: str, existing_code: str, clean_patch: str) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as sandbox_dir:
        sandbox_file_path = os.path.join(sandbox_dir, target_file)
        os.makedirs(os.path.dirname(sandbox_file_path) or sandbox_dir, exist_ok=True)

        with open(sandbox_file_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(existing_code)

        subprocess.run(["git", "init"], cwd=sandbox_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "bot@erragent.com"], cwd=sandbox_dir, capture_output=True)
        subprocess.run(["git", "config", "user.name", "errAgent"], cwd=sandbox_dir, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=sandbox_dir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=sandbox_dir, capture_output=True)

        patch_path = os.path.join(sandbox_dir, "fix.patch")
        with open(patch_path, "w", encoding="utf-8") as patch_handle:
            patch_handle.write(clean_patch)

        apply_result = subprocess.run(
            [
                "git", "apply",
                "-p1",
                "--recount",
                "--ignore-space-change",
                "--ignore-whitespace",
                "--unidiff-zero",
                "fix.patch",
            ],
            cwd=sandbox_dir,
            capture_output=True,
            text=True,
        )
        if apply_result.returncode != 0:
            raise ValueError(f"Patch apply failed in sandbox: {apply_result.stderr.strip()}")

        with open(sandbox_file_path, "r", encoding="utf-8") as file_handle:
            full_file_content = file_handle.read()

        canonical_diff_result = subprocess.run(
            ["git", "diff", "--", target_file],
            cwd=sandbox_dir,
            capture_output=True,
            text=True,
        )
        if canonical_diff_result.returncode != 0:
            raise ValueError(f"Failed to generate canonical diff: {canonical_diff_result.stderr.strip()}")

        canonical_patch = canonical_diff_result.stdout.strip() + "\n"
        if not canonical_patch.strip():
            raise ValueError("Patch applied but produced no diff; refusing to save empty remediation patch.")

        return full_file_content, canonical_patch

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
        _upsert_remediation_failure(db, incident_id, "GOOGLE_API_KEY is not configured.")
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
    fetched_branch = ""
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
                fetched_branch = branch
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
        failure_reason = (
            f"Could not fetch any target file from {target_repo}. "
            f"Candidates={target_file_candidates}"
        )
        logger.error(
            "--> [errAgent AI] CRITICAL: Could not fetch any target file from %s. Candidates=%s",
            target_repo,
            target_file_candidates,
        )
        _upsert_remediation_failure(
            db,
            incident_id,
            failure_reason,
            target_repo=target_repo,
            target_file=target_file,
        )
        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "analysis_failed", "updated_at": datetime.now(timezone.utc)}}
        )
        return

    # 2. Build base prompt with format context
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    engineering_instructions = (
        payload.get("engineering_instructions")
        or metadata.get("engineering_instructions")
        or ""
    )

    base_prompt = INCIDENT_ANALYSIS_PROMPT.format(
        service_name=payload.get("service_name", "unknown-service"),
        environment=payload.get("environment", "production"),
        stack_trace=stack_trace,
        git_diffs=payload.get("git_diffs", "No git diff context provided."),
        metadata=metadata,
        engineering_instructions=engineering_instructions,
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
        result: AIAnalysisSchema | None = None
        now = datetime.now(timezone.utc)
        full_file_content = existing_code
        canonical_patch = clean_patch = ""
        resolved_head_branch = ""
        last_error: Exception | None = None
        retry_suffix = """

CRITICAL RETRY RULES:
- Output a minimal patch with only localized hunks near the failing function.
- Do not rewrite from top-of-file. Do not emit broad file-wide changes.
- Keep patch to one target file only.
"""

        for attempt_index in range(2):
            prompt_to_use = prompt if attempt_index == 0 else f"{prompt}\n{retry_suffix}"
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=prompt_to_use,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AIAnalysisSchema,
                    temperature=0.1,
                ),
            )

            result = response.parsed
            resolved_head_branch = _build_unique_head_branch(result.head_branch, incident_id)
            clean_patch = _normalize_generated_patch(result.code_patch)
            logger.info(f"--> [errAgent AI] Generated Patch (attempt={attempt_index + 1}):\n{clean_patch}")

            try:
                if "--- /dev/null" in clean_patch:
                    raise ValueError("Patch rejected: LLM attempted to create a new file instead of modifying the existing one.")

                _validate_patch_safety(clean_patch=clean_patch, target_file=target_file, existing_code=existing_code)
                full_file_content, canonical_patch = _apply_patch_in_sandbox(
                    target_file=target_file,
                    existing_code=existing_code,
                    clean_patch=clean_patch,
                )

                original_chars = max(1, len(existing_code))
                if len(full_file_content) < int(original_chars * 0.4):
                    raise ValueError("Patch rejected: patched file is unexpectedly small relative to original file.")

                last_error = None
                break
            except Exception as patch_exc:
                last_error = patch_exc
                logger.error(
                    "--> [errAgent AI] Attempt %s patch validation/apply failed: %s",
                    attempt_index + 1,
                    str(patch_exc),
                )

        if last_error is not None or result is None:
            raise ValueError(f"Patch generation failed after retry: {str(last_error) if last_error else 'unknown error'}")

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
            "base_file_branch": fetched_branch,
            "base_file_sha256": hashlib.sha256(existing_code.encode("utf-8")).hexdigest(),
            "base_file_bytes": len(existing_code.encode("utf-8")),
            "code_patch": canonical_patch,
            "code_patch_sha256": hashlib.sha256(canonical_patch.encode("utf-8")).hexdigest(),
            "code_patch_bytes": len(canonical_patch.encode("utf-8")),
            "full_file_content": full_file_content,
            "full_file_content_sha256": hashlib.sha256(full_file_content.encode("utf-8")).hexdigest(),
            "full_file_content_bytes": len(full_file_content.encode("utf-8")),
            "content_source": "sandbox_applied",
            "base_branch": result.base_branch,
            "head_branch": resolved_head_branch,
            "head_branch_original": result.head_branch,
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
        _upsert_remediation_failure(
            db,
            incident_id,
            str(exc),
            target_repo=target_repo,
            target_file=target_file,
        )
        db["incidents"].update_one(
            {"_id": incident_id},
            {"$set": {"status": "analysis_failed", "updated_at": datetime.now(timezone.utc)}}
        )