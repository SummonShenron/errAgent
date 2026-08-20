import os
from datetime import datetime, timezone
from typing import Any

from backend.services.github_service import GitHubOpsService
from backend.services.patchy_test_planner import normalize_test_command
from backend.utils.app_utils import serialize_mongo_doc


class PatchyTestExecutionError(ValueError):
    pass


def _configured_workflow() -> str:
    workflow = os.getenv("PATCHY_TEST_WORKFLOW", "").strip()
    if not workflow:
        raise PatchyTestExecutionError(
            "No GitHub Actions test workflow is configured. "
            "Set PATCHY_TEST_WORKFLOW to an existing workflow file in the target repository."
        )
    return workflow


def _find_test_plan(test_plan_or_incident_id: str, db) -> dict[str, Any] | None:
    plan = db["patchy_test_plans"].find_one({"_id": test_plan_or_incident_id})
    if plan:
        return plan
    return db["patchy_test_plans"].find_one(
        {"incident_id": test_plan_or_incident_id, "status": "ready_for_review"},
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def create_test_execution_proposal(test_plan_id: str, actor: str, db) -> dict[str, Any]:
    plan = _find_test_plan(test_plan_id, db)
    if not plan:
        raise PatchyTestExecutionError(f"Test plan not found: {test_plan_id}. Run test plan <incident-id> first.")
    canonical_plan_id = plan["_id"]
    if plan.get("status") != "ready_for_review":
        raise PatchyTestExecutionError(f"Test plan cannot run from status: {plan.get('status', 'unknown')}")
    workflow = _configured_workflow()

    recommendations = plan.get("plan", {}).get("recommendations", [])
    commands = [normalize_test_command(item["command"]) for item in recommendations if isinstance(item.get("command"), str)]
    if not commands:
        raise PatchyTestExecutionError("Test plan contains no executable recommendations")

    now = datetime.now(timezone.utc)
    proposal = {
        "_id": f"test_run_{canonical_plan_id}",
        "kind": "github_test_workflow",
        "risk": "repository_ci_only",
        "status": "awaiting_approval",
        "summary": f"Run {len(commands)} approved test recommendation(s) on {plan['test_branch']}",
        "action": {
            "method": "POST",
            "url": f"GitHub Actions workflow: {workflow}",
            "timeoutSeconds": 30,
            "branch": plan["test_branch"],
            "commands": commands,
        },
        "testPlanId": canonical_plan_id,
        "repository": plan["repository"],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].insert_one(proposal)
    db["patchy_test_plans"].update_one(
        {"_id": canonical_plan_id},
        {"$set": {"status": "awaiting_execution_approval", "proposal_id": proposal["_id"], "updated_at": now}},
    )
    return serialize_mongo_doc(proposal)


async def approve_and_dispatch_test_plan(db, proposal_id: str, actor: str, github: GitHubOpsService) -> dict[str, Any]:
    proposal = db["patchy_proposals"].find_one({"_id": proposal_id})
    if not proposal or proposal.get("kind") != "github_test_workflow":
        raise PatchyTestExecutionError("Test execution proposal not found")
    if proposal.get("status") != "awaiting_approval":
        raise PatchyTestExecutionError(f"Proposal cannot be approved from status: {proposal.get('status', 'unknown')}")

    plan = db["patchy_test_plans"].find_one({"_id": proposal["testPlanId"]})
    if not plan or plan.get("status") != "awaiting_execution_approval":
        raise PatchyTestExecutionError("Test plan is no longer awaiting execution approval")
    started_at = datetime.now(timezone.utc)
    claimed = db["patchy_proposals"].update_one(
        {"_id": proposal_id, "status": "awaiting_approval"},
        {"$set": {"status": "running", "approved_by": actor, "approved_at": started_at, "updated_at": started_at}},
    )
    if claimed.modified_count != 1:
        raise PatchyTestExecutionError("Proposal was already claimed")

    workflow = _configured_workflow()
    result = await github.dispatch_test_workflow(
        repo=proposal["repository"],
        workflow=workflow,
        branch=proposal["action"]["branch"],
        test_commands=proposal["action"]["commands"],
    )
    completed_at = datetime.now(timezone.utc)
    succeeded = result.get("status_code") in {201, 204}
    status = "succeeded" if succeeded else "failed"
    db["patchy_proposals"].update_one(
        {"_id": proposal_id},
        {"$set": {"status": status, "result": result, "completed_at": completed_at, "updated_at": completed_at}},
    )
    db["patchy_test_plans"].update_one(
        {"_id": proposal["testPlanId"]},
        {"$set": {
            "status": "running" if succeeded else "ready_for_review",
            "workflow": workflow,
            "last_execution_error": None if succeeded else result,
            "updated_at": completed_at,
        }},
    )
    if not succeeded:
        status_code = result.get("status_code", "unknown")
        detail = result.get("data", {}).get("message") or result.get("data", {}).get("error") or f"GitHub Actions dispatch failed with HTTP {status_code}"
        if status_code == 403:
            detail = (
                "GitHub rejected workflow dispatch (403). The GITHUB_TOKEN needs access to "
                f"{proposal['repository']} with Actions: Read and write and Contents: Read, "
                "and the workflow must support workflow_dispatch. "
                f"GitHub said: {detail}"
            )
        raise PatchyTestExecutionError(detail)
    completed = serialize_mongo_doc(db["patchy_proposals"].find_one({"_id": proposal_id}))
    completed["testPlanStatus"] = "running" if succeeded else "execution_failed"
    return completed


async def get_test_execution_status(test_plan_id: str, db, github: GitHubOpsService) -> dict[str, Any]:
    plan = _find_test_plan(test_plan_id, db)
    if not plan:
        raise PatchyTestExecutionError(f"Test plan not found: {test_plan_id}. Run test plan <incident-id> first.")
    workflow = plan.get("workflow") or _configured_workflow()
    run = await github.find_latest_test_workflow_run(plan["repository"], workflow, plan["test_branch"])
    data = run.get("data", {})
    status = data.get("conclusion") or data.get("status") or "queued"
    plan_status = "passed" if status == "success" else "failed" if status in {"failure", "cancelled", "timed_out"} else "running"
    db["patchy_test_plans"].update_one(
        {"_id": test_plan_id},
        {"$set": {"status": plan_status, "workflow_run": data, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"testPlanId": test_plan_id, "status": plan_status, "workflowRun": serialize_mongo_doc(data)}
