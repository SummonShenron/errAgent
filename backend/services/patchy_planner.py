from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.utils.app_utils import serialize_mongo_doc


class PatchyPlanError(ValueError):
    pass


_PLAN_TEMPLATES = {
    ("verify", "bty", "stability"): [
        {"command": "verify bty", "reason": "Run health and latency verification with approval gates."},
        {"command": "logs bty error", "reason": "Check recent BTY error stream after health evidence."},
        {"command": "incidents", "reason": "Correlate active incidents with service evidence."},
    ],
    ("verify", "saapp", "stability"): [
        {"command": "verify saapp", "reason": "Run health and latency verification with approval gates."},
        {"command": "logs saapp error", "reason": "Check recent SAAPP error stream after health evidence."},
        {"command": "incidents", "reason": "Correlate active incidents with service evidence."},
    ],
    ("investigate", "incidents"): [
        {"command": "incidents", "reason": "List active incident surface area."},
        {"command": "logs all error", "reason": "Gather recent error telemetry."},
        {"command": "diagnostics", "reason": "Summarize health, incidents, and error logs."},
    ],
}


def create_plan(goal_text: str, actor: str, db) -> dict[str, Any]:
    tokens = tuple(goal_text.strip().lower().split())
    steps = _PLAN_TEMPLATES.get(tokens)
    if steps is None:
        raise PatchyPlanError(
            "Known plans: plan verify bty stability, plan verify saapp stability, plan investigate incidents"
        )

    now = datetime.now(timezone.utc)
    document = {
        "_id": f"plan_{uuid4().hex}",
        "goal": goal_text.strip(),
        "status": "ready",
        "nextStepIndex": 0,
        "steps": [
            {"index": index, "status": "pending", **step}
            for index, step in enumerate(steps)
        ],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_plans"].insert_one(document)
    return serialize_mongo_doc(document)


def create_incident_investigation_plan(incident_id: str, actor: str, db) -> dict[str, Any]:
    incident = db["incidents"].find_one({"_id": incident_id})
    if not incident:
        raise PatchyPlanError(f"Incident not found: {incident_id}")

    status = str(incident.get("status") or "open").lower()
    is_resolved = status in {"resolved", "closed"}
    goal = f"review resolved incident {incident_id}" if is_resolved else f"investigate incident {incident_id}"
    steps = [
        {"command": f"explain {incident_id}", "reason": "Summarize incident details, analysis, and suggested fix."},
    ]

    now = datetime.now(timezone.utc)
    document = {
        "_id": f"plan_{uuid4().hex}",
        "goal": goal,
        "status": "ready",
        "nextStepIndex": 0,
        "kind": "resolution_review" if is_resolved else "incident_investigation",
        "subject": {
            "incidentId": incident_id,
            "status": status,
            "service": "bty" if "bty" in str(incident.get("service_name", "")).lower() else "saapp" if "saapp" in str(incident.get("service_name", "")).lower() else "",
        },
        "steps": [
            {"index": index, "status": "pending", **step}
            for index, step in enumerate(steps)
        ],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_plans"].insert_one(document)
    return serialize_mongo_doc(document)


def get_plan(db, plan_id: str) -> dict[str, Any]:
    plan = db["patchy_plans"].find_one({"_id": plan_id})
    if not plan:
        raise PatchyPlanError(f"Plan not found: {plan_id}")
    return plan


def get_latest_active_plan(db, actor: str) -> dict[str, Any]:
    plan = db["patchy_plans"].find_one(
        {"created_by": actor, "status": {"$ne": "completed"}},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if not plan:
        plan = db["patchy_plans"].find_one(
            {"status": {"$ne": "completed"}},
            sort=[("updated_at", -1), ("created_at", -1)],
        )
    if not plan:
        raise PatchyPlanError("No active plan found. Create one with 'plan verify bty stability'.")
    return plan


def record_step_result(db, plan_id: str, step_index: int, result: dict[str, Any]) -> dict[str, Any]:
    plan = get_plan(db, plan_id)
    steps = plan.get("steps", [])
    if step_index >= len(steps):
        raise PatchyPlanError("Plan is already complete")

    steps[step_index]["status"] = "completed"
    steps[step_index]["result"] = result
    next_index = step_index + 1
    status = "completed" if next_index >= len(steps) else "ready"
    now = datetime.now(timezone.utc)
    db["patchy_plans"].update_one(
        {"_id": plan_id},
        {"$set": {"steps": steps, "nextStepIndex": next_index, "status": status, "updated_at": now}},
    )
    return serialize_mongo_doc(get_plan(db, plan_id))


def _investigation_follow_up(plan: dict[str, Any], result: dict[str, Any]) -> dict[str, str] | None:
    if plan.get("kind") not in {"incident_investigation", "resolution_review"}:
        return None

    subject = plan.get("subject", {})
    incident_status = str(subject.get("status") or "open").lower()
    if incident_status in {"resolved", "closed"}:
        return None

    service = str(subject.get("service") or "").lower()
    result_lines = " ".join(str(line) for line in result.get("lines", []))
    completed_steps = [step for step in plan.get("steps", []) if step.get("status") == "completed"]
    last_command = str(completed_steps[-1].get("command", "")) if completed_steps else ""
    if last_command.startswith("explain "):
        if "not analyzed yet" in result_lines.lower():
            return {
                "command": "diagnostics",
                "reason": "No root-cause analysis exists yet; collect broad health and telemetry evidence.",
            }
        return {
            "command": f"logs {service} error" if service in {"bty", "saapp"} else "logs all error",
            "reason": "Use the incident service and current analysis to find matching runtime failures.",
        }

    if result.get("status") in {"warning", "error"}:
        return {
            "command": "diagnostics",
            "reason": "Prior evidence found an operational concern; reassess health and active incidents.",
        }
    return None


def record_adaptive_step_result(db, plan_id: str, step_index: int, result: dict[str, Any]) -> dict[str, Any]:
    updated_plan = record_step_result(db, plan_id, step_index, result)
    steps = updated_plan.get("steps", [])
    follow_up = _investigation_follow_up(updated_plan, result)
    if not follow_up or any(step.get("command") == follow_up["command"] for step in steps):
        return updated_plan

    next_index = len(steps)
    steps.append({"index": next_index, "status": "pending", **follow_up})
    now = datetime.now(timezone.utc)
    db["patchy_plans"].update_one(
        {"_id": plan_id},
        {"$set": {"steps": steps, "status": "ready", "updated_at": now}},
    )
    return serialize_mongo_doc(get_plan(db, plan_id))


def build_plan_report(plan: dict[str, Any]) -> dict[str, Any] | None:
    if plan.get("status") != "completed":
        return None
    if plan.get("kind") not in {"incident_investigation", "resolution_review"}:
        return None

    subject = plan.get("subject", {})
    steps = plan.get("steps", [])
    warning_count = sum(1 for step in steps if (step.get("result") or {}).get("status") == "warning")
    error_count = sum(1 for step in steps if (step.get("result") or {}).get("status") == "error")
    is_resolution = plan.get("kind") == "resolution_review"
    title = (
        f"Resolution review complete: {subject.get('incidentId')}"
        if is_resolution
        else f"Investigation complete: {subject.get('incidentId')}"
    )
    status = "error" if error_count else "warning" if warning_count else "success"
    lines = [
        f"Incident status at planning: {subject.get('status', 'unknown')}",
        f"Evidence steps completed: {len(steps)}",
        f"Warnings found: {warning_count}",
        f"Errors found: {error_count}",
        "Recommendation: continue monitoring for matching errors." if is_resolution else "Recommendation: review analysis and remediate before closing.",
    ]
    return {"status": status, "title": title, "lines": lines}


def next_step(plan: dict[str, Any]) -> dict[str, Any]:
    index = int(plan.get("nextStepIndex", 0))
    steps = plan.get("steps", [])
    if index >= len(steps):
        raise PatchyPlanError("Plan is already complete")
    return steps[index]
