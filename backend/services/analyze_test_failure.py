from backend.services.github_service import GitHubOpsService
from backend.services.patchy_hitl import resolve_repository
from backend.services.patchy_reasoning import PatchyReasoningError, synthesize_incident
from backend.prompts.constraints import PATCHY_TEST_FAILURE_ANALYSIS_PROMPT
from typing import Any
import json

async def analyze_test_failure(test_plan_id: str, db, broker) -> dict[str, Any]:
    # 1. Look up test plan → repo + workflow info
    plan = db["patchy_test_plans"].find_one({"_id": test_plan_id})
    if not plan:
        raise PatchyReasoningError(f"Test plan not found: {test_plan_id}")

    service_name = (plan.get("subject") or {}).get("serviceName", "unknown")
    environment = (plan.get("subject") or {}).get("environment", "ci")
    repo = plan.get("repository") or resolve_repository(service_name)

    # 2. Fetch latest CI run + logs
    gh = GitHubOpsService()
    ci_result = await gh.get_latest_test_result(repo, plan.get("workflowId"))
    stack_trace = ci_result.get("failure_log", "")
    target_file_path = ci_result.get("target_file_path", "unknown")
    git_diffs = ci_result.get("git_diffs", "")
    metadata = {
        "branch": ci_result.get("branch"),
        "commit": ci_result.get("commit_sha"),
        "workflow": ci_result.get("workflow_name"),
    }

    # 3. Build prompt for LLM
    prompt = PATCHY_TEST_FAILURE_ANALYSIS_PROMPT.format(
        service_name=service_name,
        environment=environment,
        target_file_path=target_file_path,
        failure_log=ci_result.get("failure_log", ""),
        stack_trace=stack_trace,
        git_diffs=git_diffs,
        metadata=json.dumps(metadata, indent=2),
    )


    # 4. Call Patchy’s reasoning/LLM layer
    result = await synthesize_incident(
        incident_id=f"test_failure_{test_plan_id}",
        db=db,
        broker=broker,
        prompt_override=prompt,
    )

    # 5. Shape operator-facing explanation
    synthesis = result["synthesis"]
    lines = [
        synthesis["summary"],
        "",
        "Root cause:",
        f"- {synthesis.get('root_cause', 'Not explicitly extracted')}",
        "",
        "Proposed fix (high level):",
        f"- {synthesis.get('suggested_fix', 'Not available')}",
        "",
        "Example patch (if provided):",
        *(synthesis.get("example_patch_lines") or ["- None generated"]),
    ]

    return {
        "kind": "test_failure_analysis",
        "testPlanId": test_plan_id,
        "lines": lines,
        "synthesis": synthesis,
    }
