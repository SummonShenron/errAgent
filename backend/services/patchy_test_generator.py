import asyncio
import ast
import os
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from google import genai
from google.genai import types

from backend.app.models.patchy_generated_test_models import PatchyGeneratedTest
from backend.prompts.constraints import PATCHY_REGRESSION_TEST_PROMPT
from backend.services.github_service import GitHubOpsService
from backend.utils.app_utils import serialize_mongo_doc


class PatchyGeneratedTestError(ValueError):
    pass


def _validate_generated_test(test: PatchyGeneratedTest) -> None:
    if not re.fullmatch(r"(?:tests/)?(?:test_[A-Za-z0-9_-]+|[A-Za-z0-9_-]+_test)\.py", test.test_file):
        raise PatchyGeneratedTestError("Generated test path must be a new tests/*.py file")
    if not re.fullmatch(r"test_[A-Za-z_][A-Za-z0-9_]*", test.test_name):
        raise PatchyGeneratedTestError("Generated test name must begin with test_")
    if len(test.content) > 12000 or "```" in test.content:
        raise PatchyGeneratedTestError("Generated test content is too large or contains markdown")
    try:
        tree = ast.parse(test.content)
    except SyntaxError as exc:
        raise PatchyGeneratedTestError(f"Generated test is not valid Python: {exc.msg}") from exc
    source = test.content.lower()
    forbidden = ("subprocess", "os.system", "eval(", "exec(", "pip install", "requests.get", "httpx.get", "time.sleep")
    if any(token in source for token in forbidden):
        raise PatchyGeneratedTestError("Generated test contains a forbidden side effect")
    if "assert" not in source:
        raise PatchyGeneratedTestError("Generated test must contain an assertion")
    function_names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if test.test_name not in function_names:
        raise PatchyGeneratedTestError("Generated test name is not present in content")


async def generate_regression_test(incident_id: str, db, github: GitHubOpsService) -> dict[str, Any]:
    incident = db["incidents"].find_one({"_id": incident_id})
    if not incident:
        raise PatchyGeneratedTestError(f"Incident not found: {incident_id}")
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    repo = str(incident.get("repository") or os.getenv("DEFAULT_TARGET_REPO", "")).strip()
    branch = str(remediation.get("head_branch") or "")
    if not repo or not branch:
        raise PatchyGeneratedTestError("A repository and hotfix branch are required before generating a regression test")
    base_branch = str(remediation.get("base_branch") or "main")
    try:
        diff, repository_context = await asyncio.gather(
            github.fetch_branch_diff(repo, base_branch, branch),
            github.fetch_repository_context(repo, branch),
        )
        source_files = [path for path in diff.get("files_changed", []) if path.endswith(".py")][:4]
        test_files = [path for path in repository_context.get("testFiles", [])][:4]
        files = await github.fetch_repository_files(repo, branch, list(dict.fromkeys(source_files + test_files))[:8])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise PatchyGeneratedTestError(
                f"Hotfix branch '{branch}' or repository '{repo}' was not found on GitHub. "
                "Approve the hotfix first and verify the remediation branch exists."
            ) from exc
        raise PatchyGeneratedTestError(
            f"GitHub could not read the hotfix branch '{branch}' (HTTP {exc.response.status_code})."
        ) from exc
    except httpx.HTTPError as exc:
        raise PatchyGeneratedTestError("GitHub could not be reached while reading the hotfix branch.") from exc
    evidence = serialize_mongo_doc({"incident": incident, "remediation": remediation, "diff": diff, "hotfixFiles": files})
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise PatchyGeneratedTestError("GOOGLE_API_KEY is not configured for test generation")
    client = genai.Client(api_key=api_key)
    llm_timeout_seconds = int(os.getenv("PATCHY_LLM_TIMEOUT_SECONDS", "60"))
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model=os.getenv("PATCHY_CODE_MODEL") or os.getenv("PATCHY_REASONING_MODEL", "gemini-3.5-flash"),
                contents=PATCHY_REGRESSION_TEST_PROMPT.format(evidence=evidence),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=PatchyGeneratedTest,
                    temperature=0.1,
                ),
            ),
            timeout=llm_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise PatchyGeneratedTestError(
            f"Patchy test generation timed out after {llm_timeout_seconds}s"
        ) from exc
    generated = response.parsed
    if not isinstance(generated, PatchyGeneratedTest):
        raise PatchyGeneratedTestError("Patchy returned no valid generated test")
    _validate_generated_test(generated)
    if generated.test_file in files:
        raise PatchyGeneratedTestError("Generated test would overwrite an existing repository file")

    now = datetime.now(timezone.utc)
    document = {
        "_id": f"generated_test_{uuid4().hex}",
        "incident_id": incident_id,
        "repository": repo,
        "base_branch": base_branch,
        "test_branch": branch,
        "test_file": generated.test_file,
        "test_name": generated.test_name,
        "rationale": generated.rationale,
        "content": generated.content,
        "status": "ready_for_review",
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_generated_tests"].insert_one(document)
    return serialize_mongo_doc(document)


def create_generated_test_proposal(generated_test_id: str, actor: str, db) -> dict[str, Any]:
    generated = db["patchy_generated_tests"].find_one({"_id": generated_test_id})
    if not generated:
        raise PatchyGeneratedTestError(f"Generated test not found: {generated_test_id}")
    if generated.get("status") != "ready_for_review":
        raise PatchyGeneratedTestError(f"Generated test cannot be approved from status: {generated.get('status', 'unknown')}")
    now = datetime.now(timezone.utc)
    proposal = {
        "_id": f"generated_test_commit_{generated_test_id}",
        "kind": "generated_test_commit",
        "risk": "hotfix_branch_test_only",
        "status": "awaiting_approval",
        "summary": f"Add regression test {generated['test_file']} to hotfix branch",
        "action": {
            "method": "PUT",
            "url": f"GitHub file: {generated['test_file']}",
            "branch": generated["test_branch"],
            "file": generated["test_file"],
            "content": generated["content"],
        },
        "generatedTestId": generated_test_id,
        "repository": generated["repository"],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].insert_one(proposal)
    db["patchy_generated_tests"].update_one(
        {"_id": generated_test_id},
        {"$set": {"status": "awaiting_approval", "proposal_id": proposal["_id"], "updated_at": now}},
    )
    return serialize_mongo_doc(proposal)


async def approve_and_commit_generated_test(db, proposal_id: str, actor: str, github: GitHubOpsService) -> dict[str, Any]:
    proposal = db["patchy_proposals"].find_one({"_id": proposal_id})
    if not proposal or proposal.get("kind") != "generated_test_commit":
        raise PatchyGeneratedTestError("Generated-test proposal not found")
    if proposal.get("status") != "awaiting_approval":
        raise PatchyGeneratedTestError(f"Proposal cannot be approved from status: {proposal.get('status', 'unknown')}")
    generated = db["patchy_generated_tests"].find_one({"_id": proposal["generatedTestId"]})
    if not generated or generated.get("status") != "awaiting_approval":
        raise PatchyGeneratedTestError("Generated test is no longer awaiting approval")
    claimed = db["patchy_proposals"].update_one(
        {"_id": proposal_id, "status": "awaiting_approval"},
        {"$set": {"status": "running", "approved_by": actor, "approved_at": datetime.now(timezone.utc)}},
    )
    if claimed.modified_count != 1:
        raise PatchyGeneratedTestError("Proposal was already claimed")

    try:
        commit = await github.create_branch_and_commit(
            repo=generated["repository"],
            base_branch=generated["test_branch"],
            new_branch=generated["test_branch"],
            file_path=generated["test_file"],
            file_content=generated["content"],
            commit_message=f"Add regression test for incident {generated['incident_id']}",
        )
        status = "succeeded"
        result = {"commit": commit, "branch": generated["test_branch"], "file": generated["test_file"]}
    except Exception as exc:
        status = "failed"
        result = {"error": str(exc)}
    completed_at = datetime.now(timezone.utc)
    db["patchy_proposals"].update_one(
        {"_id": proposal_id},
        {"$set": {"status": status, "result": result, "completed_at": completed_at, "updated_at": completed_at}},
    )
    db["patchy_generated_tests"].update_one(
        {"_id": generated["_id"]},
        {"$set": {"status": "committed" if status == "succeeded" else "commit_failed", "updated_at": completed_at}},
    )
    return serialize_mongo_doc(db["patchy_proposals"].find_one({"_id": proposal_id}))
