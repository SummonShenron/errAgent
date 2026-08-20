import os
import re
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4

from google import genai
from google.genai import types

from backend.app.models.patchy_test_models import PatchyTestPlan
from backend.prompts.constraints import PATCHY_TEST_PLAN_PROMPT
from backend.services.github_service import GitHubOpsService
from backend.utils.app_utils import serialize_mongo_doc


class PatchyTestPlanError(ValueError):
    pass


_PYTEST_COMMAND = re.compile(r"^(?:pytest|python -m pytest) (?P<file>[^\s:]+)(?P<nodes>(::[A-Za-z_][A-Za-z0-9_]*)*)$")


def normalize_test_command(command: str) -> str:
    normalized = " ".join(command.strip().split())
    if normalized.startswith("pytest "):
        return f"python -m {normalized}"
    return normalized


def _validate_test_command(command: str, test_files: set[str]) -> bool:
    normalized = normalize_test_command(command)
    if any(token in normalized for token in (";", "&&", "||", "|", ">", "<", "`", "$(", "\\")):
        return False
    match = _PYTEST_COMMAND.fullmatch(normalized)
    return bool(match and match.group("file") in test_files)


async def create_test_plan(incident_id: str, db, github: GitHubOpsService) -> dict[str, Any]:
    incident = db["incidents"].find_one({"_id": incident_id})
    if not incident:
        raise PatchyTestPlanError(f"Incident not found: {incident_id}")
    repo = str(incident.get("repository") or os.getenv("DEFAULT_TARGET_REPO", "")).strip()
    if not repo:
        raise PatchyTestPlanError("Repository is missing from the incident")

    analysis = db["analyses"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    branch = str(remediation.get("head_branch") or remediation.get("base_file_branch") or "main")
    base_branch = str(remediation.get("base_branch") or "main")
    branch_source = "hotfix" if remediation.get("head_branch") else "base"
    repository_context = await github.fetch_repository_context(repo, branch)
    test_files = repository_context.get("testFiles", [])
    if not test_files:
        raise PatchyTestPlanError("No repository test files were found on the configured base branch")
    file_contents = await github.fetch_repository_files(repo, branch, test_files[:8])
    diff_context = await github.fetch_branch_diff(repo, base_branch, branch) if branch != base_branch else {"commit_count": 0, "commits": [], "files_changed": []}

    evidence = serialize_mongo_doc({
        "incident": incident,
        "analysis": analysis,
        "remediation": remediation,
        "repository": {"name": repo, "branch": branch, "baseBranch": base_branch, "branchSource": branch_source, "testFiles": test_files, "diff": diff_context},
        "testFileContents": file_contents,
    })
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise PatchyTestPlanError("GOOGLE_API_KEY is not configured for Patchy test planning")

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=os.getenv("PATCHY_REASONING_MODEL", "gemini-3.5-flash"),
        contents=PATCHY_TEST_PLAN_PROMPT.format(evidence=evidence),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PatchyTestPlan,
            temperature=0.1,
        ),
    )
    plan = response.parsed
    if not isinstance(plan, PatchyTestPlan):
        raise PatchyTestPlanError("Patchy test planning returned no valid structured plan")
    allowed_files = set(test_files)
    invalid_commands = [
        recommendation.command
        for recommendation in plan.recommendations
        if not _validate_test_command(recommendation.command, allowed_files)
    ]
    if invalid_commands:
        raise PatchyTestPlanError(
            "Patchy returned an unsupported pytest command: "
            + ", ".join(repr(command) for command in invalid_commands[:3])
        )
    valid_recommendations = [
        recommendation.model_dump()
        for recommendation in plan.recommendations
    ]
    result = {
        "incidentId": incident_id,
        "repository": repo,
        "branch": branch,
        "baseBranch": base_branch,
        "branchSource": branch_source,
        "diff": diff_context,
        "plan": plan.model_dump(),
        "repositoryContext": {"testFiles": test_files, "fetchedFileCount": len(file_contents)},
    }
    plan_document = {
        "_id": f"testplan_{uuid4().hex}",
        "incident_id": incident_id,
        "repository": repo,
        "base_branch": base_branch,
        "test_branch": branch,
        "branch_source": branch_source,
        "diff": diff_context,
        "plan": result["plan"],
        "status": "ready_for_review",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    db["patchy_test_plans"].insert_one(plan_document)
    db["remediations"].update_one(
        {"incident_id": incident_id},
        {"$set": {"test_plan_id": plan_document["_id"], "updated_at": plan_document["updated_at"]}},
    )
    result["testPlanId"] = plan_document["_id"]
    return result
