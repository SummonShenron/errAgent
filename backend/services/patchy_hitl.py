import asyncio
import statistics
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import requests

from backend.services.synthetic_adapters import SyntheticAdapterError, get_synthetic_adapter
from backend.utils.app_utils import SERVICES, serialize_mongo_doc
from backend.services.patchy_planner import (
    PatchyPlanError,
    get_latest_active_plan,
    get_plan,
    next_step,
    record_adaptive_step_result,
)


class PatchyProposalError(ValueError):
    pass


_SERVICE_ALIASES = {
    "bty": "BTY Fitness",
    "saapp": "SAAPP Widget",
}


def _service_by_alias(alias: str) -> dict[str, Any]:
    service_name = _SERVICE_ALIASES.get(alias.lower())
    if not service_name:
        raise PatchyProposalError("Usage: probe [bty|saapp]")
    service = next((item for item in SERVICES if item["name"] == service_name), None)
    if not service:
        raise PatchyProposalError(f"Service is not registered: {service_name}")
    return service


def create_probe_proposal(alias: str, actor: str, db) -> dict[str, Any]:
    service = _service_by_alias(alias)
    now = datetime.now(timezone.utc)
    proposal_id = f"patchy_{uuid4().hex}"
    url = service["url"].rstrip("/") + service.get("health_path", "/")
    document = {
        "_id": proposal_id,
        "kind": "http_probe",
        "risk": "read_only",
        "status": "awaiting_approval",
        "summary": f"Probe {service['name']} health endpoint",
        "action": {
            "method": "GET",
            "url": url,
            "timeoutSeconds": 15,
            "allowedStatusCodes": list(range(200, 500)),
        },
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].insert_one(document)
    return serialize_mongo_doc(document)


def create_synthetic_proposal(alias: str, actor: str, db) -> dict[str, Any]:
    service = _service_by_alias(alias)
    now = datetime.now(timezone.utc)
    proposal = {
        "_id": f"synthetic_{uuid4().hex}",
        "kind": "synthetic_http",
        "risk": "registered_read_only",
        "status": "awaiting_approval",
        "summary": f"Run a synthetic health assertion for {service['name']}",
        "action": {
            "method": "GET",
            "url": service["url"].rstrip("/") + service.get("health_path", "/"),
            "timeoutSeconds": 15,
            "assertions": ["HTTP status is 2xx", "response completes within timeout"],
        },
        "serviceAlias": alias.lower(),
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].insert_one(proposal)
    return serialize_mongo_doc(proposal)


def create_verification_workflow(alias: str, actor: str, db) -> dict[str, Any]:
    service = _service_by_alias(alias)
    workflow_id = f"verify_{uuid4().hex}"
    proposal = create_probe_proposal(alias, actor, db)
    db["patchy_proposals"].update_one(
        {"_id": proposal["_id"]},
        {
            "$set": {
                "workflow": {
                    "id": workflow_id,
                    "goal": f"Verify {service['name']} stability",
                    "serviceAlias": alias.lower(),
                    "step": 1,
                    "totalSteps": 2,
                }
            }
        },
    )
    proposal["workflow"] = {
        "id": workflow_id,
        "goal": f"Verify {service['name']} stability",
        "serviceAlias": alias.lower(),
        "step": 1,
        "totalSteps": 2,
    }
    return proposal


def _create_latency_proposal(previous: dict[str, Any], actor: str, db) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    proposal_id = f"patchy_{uuid4().hex}"
    workflow = dict(previous["workflow"])
    workflow["step"] = 2
    document = {
        "_id": proposal_id,
        "kind": "latency_probe",
        "risk": "read_only",
        "status": "awaiting_approval",
        "summary": f"Sample {workflow['serviceAlias'].upper()} latency five times",
        "action": {
            "method": "GET",
            "url": previous["action"]["url"],
            "timeoutSeconds": 15,
            "samples": 5,
        },
        "workflow": workflow,
        "previousProposalId": previous["_id"],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].insert_one(document)
    return serialize_mongo_doc(document)


def get_proposal(db, proposal_id: str) -> dict[str, Any]:
    proposal = db["patchy_proposals"].find_one({"_id": proposal_id})
    if not proposal:
        raise PatchyProposalError(f"Proposal not found: {proposal_id}")
    return proposal


def list_proposals(db, limit: int = 20) -> list[dict[str, Any]]:
    proposals = list(db["patchy_proposals"].find({}).sort("created_at", -1).limit(limit))
    return serialize_mongo_doc(proposals)


def create_plan_step_proposal(db, actor: str, plan_id: str | None = None) -> dict[str, Any]:
    resolved_plan_id = plan_id
    if plan_id and not str(plan_id).startswith("plan_"):
        plan_from_incident = db["patchy_plans"].find_one(
            {
                "subject.incidentId": plan_id,
                "status": {"$ne": "completed"},
            },
            sort=[("updated_at", -1), ("created_at", -1)],
        )
        if not plan_from_incident:
            raise PatchyProposalError(
                f"No active plan found for incident: {plan_id}. Run investigate {plan_id} or plan verify ... first."
            )
        resolved_plan_id = plan_from_incident["_id"]

    try:
        plan = get_plan(db, resolved_plan_id) if resolved_plan_id else get_latest_active_plan(db, actor)
        step = next_step(plan)
    except PatchyPlanError as exc:
        raise PatchyProposalError(str(exc)) from exc

    now = datetime.now(timezone.utc)
    proposal_id = f"plan_step_{plan['_id']}_{step['index']}"
    existing = db["patchy_proposals"].find_one({"_id": proposal_id})
    if existing and existing.get("status") == "awaiting_approval":
        return serialize_mongo_doc(existing)

    proposal = {
        "_id": proposal_id,
        "kind": "plan_step",
        "risk": "allowlisted_command_only",
        "status": "awaiting_approval",
        "summary": f"Run plan step {step['index'] + 1}: {step['command']}",
        "action": {
            "method": "PATCHY",
            "url": f"patchy://command/{step['command']}",
            "command": step["command"],
            "reason": step.get("reason", ""),
        },
        "planId": plan["_id"],
        "planStepIndex": step["index"],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].replace_one({"_id": proposal_id}, proposal, upsert=True)
    return serialize_mongo_doc(proposal)


def decline_plan_step_proposal(db, proposal_id: str, actor: str) -> dict[str, Any]:
    proposal = get_proposal(db, proposal_id)
    if proposal.get("kind") != "plan_step":
        raise PatchyProposalError("Only guided plan-step proposals can be declined")
    if proposal.get("status") != "awaiting_approval":
        raise PatchyProposalError(f"Proposal cannot be declined from status: {proposal.get('status', 'unknown')}")

    now = datetime.now(timezone.utc)
    db["patchy_proposals"].update_one(
        {"_id": proposal_id, "status": "awaiting_approval"},
        {
            "$set": {
                "status": "declined",
                "declined_by": actor,
                "declined_at": now,
                "updated_at": now,
                "result": {"reason": "operator_declined"},
            }
        },
    )
    return serialize_mongo_doc(db["patchy_proposals"].find_one({"_id": proposal_id}))


async def approve_and_execute_probe(
    db,
    proposal_id: str,
    actor: str,
    broker=None,
    outbound_bearer_token: str | None = None,
) -> dict[str, Any]:
    proposal = get_proposal(db, proposal_id)
    if proposal.get("status") != "awaiting_approval":
        raise PatchyProposalError(
            f"Proposal cannot be approved from status: {proposal.get('status', 'unknown')}"
        )
    if proposal.get("kind") == "synthetic_question":
        return await _approve_and_execute_synthetic_question(
            db,
            proposal,
            actor,
            outbound_bearer_token=outbound_bearer_token,
        )
    if proposal.get("kind") == "plan_step":
        return await _approve_and_execute_plan_step(db, proposal, actor, broker)
    if proposal.get("kind") not in {"http_probe", "latency_probe", "synthetic_http"} or proposal.get("risk") not in {"read_only", "registered_read_only"}:
        raise PatchyProposalError("Only registered read-only probes and synthetic checks are supported")

    action = proposal.get("action") or {}
    if action.get("method") != "GET":
        raise PatchyProposalError("Policy rejected non-GET probe")

    registered_urls = {
        service["url"].rstrip("/") + service.get("health_path", "/")
        for service in SERVICES
    }
    url = action.get("url")
    if url not in registered_urls:
        raise PatchyProposalError("Policy rejected an unregistered destination")

    started_at = datetime.now(timezone.utc)
    claimed = db["patchy_proposals"].update_one(
        {"_id": proposal_id, "status": "awaiting_approval"},
        {
            "$set": {
                "status": "running",
                "approved_by": actor,
                "approved_at": started_at,
                "updated_at": started_at,
            }
        },
    )
    if claimed.modified_count != 1:
        raise PatchyProposalError("Proposal was already claimed")

    try:
        sample_count = min(max(int(action.get("samples", 1)), 1), 10)
        responses = []
        for _ in range(sample_count):
            responses.append(
                await asyncio.to_thread(
                    requests.get,
                    url,
                    timeout=min(int(action.get("timeoutSeconds", 15)), 30),
                )
            )

        completed_at = datetime.now(timezone.utc)
        elapsed_values = [round(item.elapsed.total_seconds() * 1000) for item in responses]
        if proposal["kind"] == "latency_probe":
            result = {
                "samples": elapsed_values,
                "medianMs": round(statistics.median(elapsed_values)),
                "maxMs": max(elapsed_values),
                "statusCodes": [item.status_code for item in responses],
            }
            final_status = "succeeded" if all(200 <= item.status_code < 400 for item in responses) else "failed"
        else:
            response = responses[0]
            try:
                response_body: Any = response.json()
            except Exception:
                response_body = response.text[:4000]
            result = {
                "httpStatus": response.status_code,
                "elapsedMs": elapsed_values[0],
                "body": response_body,
            }
            final_status = "succeeded" if 200 <= response.status_code < 400 else "failed"
        db["patchy_proposals"].update_one(
            {"_id": proposal_id},
            {
                "$set": {
                    "status": final_status,
                    "result": result,
                    "completed_at": completed_at,
                    "updated_at": completed_at,
                }
            },
        )
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        result = {"error": str(exc)}
        final_status = "failed"
        db["patchy_proposals"].update_one(
            {"_id": proposal_id},
            {
                "$set": {
                    "status": final_status,
                    "result": result,
                    "completed_at": completed_at,
                    "updated_at": completed_at,
                }
            },
        )

    completed = serialize_mongo_doc(get_proposal(db, proposal_id))
    workflow = proposal.get("workflow")
    if not workflow:
        return completed

    if proposal["kind"] == "http_probe" and completed["status"] == "succeeded":
        completed["nextProposal"] = _create_latency_proposal(proposal, actor, db)
        completed["workflowStatus"] = "awaiting_approval"
        return completed

    service_alias = workflow["serviceAlias"]
    service_name = _SERVICE_ALIASES[service_alias]
    incidents = list(
        db["incidents"].find(
            {
                "status": {"$nin": ["resolved", "closed"]},
                "service_name": {"$regex": service_alias, "$options": "i"},
            },
            {"_id": 1, "status": 1, "error_message": 1},
        ).limit(10)
    )
    error_logs = await broker.get_history(service=service_alias.upper(), level="error", limit=10) if broker else []
    median_ms = completed.get("result", {}).get("medianMs")
    health_ok = completed["status"] == "succeeded"
    stability = "STABLE"
    if not health_ok or incidents or error_logs:
        stability = "UNHEALTHY"
    elif isinstance(median_ms, (int, float)) and median_ms >= 2000:
        stability = "DEGRADED"

    completed["workflowStatus"] = "completed"
    completed["workflowReport"] = {
        "status": "success" if stability == "STABLE" else "warning",
        "title": f"{service_name} stability: {stability}",
        "lines": [
            f"Health samples: {'passed' if health_ok else 'failed'}",
            f"Median latency: {median_ms if median_ms is not None else 'n/a'}ms",
            f"Active incidents: {len(incidents)}",
            f"Recent error logs: {len(error_logs)}",
            "Recommendation: continue normal monitoring." if stability == "STABLE" else "Recommendation: investigate flagged evidence before declaring stability.",
        ],
        "data": serialize_mongo_doc({"incidents": incidents, "errorLogs": error_logs}),
    }
    return completed


async def _approve_and_execute_synthetic_question(
    db,
    proposal: dict[str, Any],
    actor: str,
    outbound_bearer_token: str | None = None,
) -> dict[str, Any]:
    action = proposal.get("action") or {}
    try:
        adapter = get_synthetic_adapter(
            proposal.get("adapter", ""),
            allow_production=proposal.get("risk") == "production_read_only",
        )
    except SyntheticAdapterError as exc:
        raise PatchyProposalError(str(exc)) from exc
    if action.get("method") != "POST" or action.get("url") != adapter["url"]:
        raise PatchyProposalError("Synthetic question failed its adapter policy")
    if action.get("environment") not in {"staging", "production"}:
        raise PatchyProposalError("Synthetic question has an unsupported environment")
    if action.get("environment") == "production" and proposal.get("risk") != "production_read_only":
        raise PatchyProposalError("Production synthetic question is not marked read-only")

    claimed = db["patchy_proposals"].update_one(
        {"_id": proposal["_id"], "status": "awaiting_approval"},
        {"$set": {"status": "running", "approved_by": actor, "approved_at": datetime.now(timezone.utc)}},
    )
    if claimed.modified_count != 1:
        raise PatchyProposalError("Proposal was already claimed")
    try:
        request_headers = dict(adapter.get("headers") or {})
        if outbound_bearer_token and "Authorization" not in request_headers:
            request_headers["Authorization"] = f"Bearer {outbound_bearer_token}"
        response = await asyncio.to_thread(
            requests.post,
            adapter["url"],
            json={"question": action["question"]},
            headers=request_headers or None,
            timeout=min(int(action.get("timeoutSeconds", 30)), 30),
        )
        try:
            body: Any = response.json()
        except Exception:
            body = response.text[:4000]
        answer = body.get("answer") if isinstance(body, dict) else body
        result = {
            "question": action["question"],
            "httpStatus": response.status_code,
            "body": body,
            "answer": answer,
            "hasAnswer": bool(str(answer or "").strip()),
        }
        if not (200 <= response.status_code < 300):
            detail = None
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("error") or body.get("message")
            elif isinstance(body, str):
                detail = body.strip()[:500]
            if detail:
                result["error"] = f"HTTP {response.status_code}: {detail}"
        final_status = "succeeded" if 200 <= response.status_code < 300 and result["hasAnswer"] else "failed"
    except Exception as exc:
        result = {"error": str(exc)}
        final_status = "failed"
    completed_at = datetime.now(timezone.utc)
    db["patchy_proposals"].update_one(
        {"_id": proposal["_id"]},
        {"$set": {"status": final_status, "result": result, "completed_at": completed_at, "updated_at": completed_at}},
    )
    return serialize_mongo_doc(db["patchy_proposals"].find_one({"_id": proposal["_id"]}))


async def _approve_and_execute_plan_step(db, proposal: dict[str, Any], actor: str, broker=None) -> dict[str, Any]:
    from backend.services.patchy_terminal import execute_patchy_command

    command = str((proposal.get("action") or {}).get("command") or "").strip()
    if not command:
        raise PatchyProposalError("Plan step proposal is missing a command")

    plan_id = proposal.get("planId")
    expected_step_index = proposal.get("planStepIndex")
    try:
        plan = get_plan(db, plan_id)
        step = next_step(plan)
    except PatchyPlanError as exc:
        raise PatchyProposalError(str(exc)) from exc

    if int(step.get("index", -1)) != int(expected_step_index):
        raise PatchyProposalError("Plan changed since this proposal was created. Request a new guided step.")
    if step.get("command") != command:
        raise PatchyProposalError("Plan step command no longer matches proposal command")

    started_at = datetime.now(timezone.utc)
    claimed = db["patchy_proposals"].update_one(
        {"_id": proposal["_id"], "status": "awaiting_approval"},
        {
            "$set": {
                "status": "running",
                "approved_by": actor,
                "approved_at": started_at,
                "updated_at": started_at,
            }
        },
    )
    if claimed.modified_count != 1:
        raise PatchyProposalError("Proposal was already claimed")

    try:
        result = await execute_patchy_command(command, db, broker, actor=actor)
        updated_plan = record_adaptive_step_result(db, plan_id, int(expected_step_index), result)
        completed_at = datetime.now(timezone.utc)
        db["patchy_proposals"].update_one(
            {"_id": proposal["_id"]},
            {
                "$set": {
                    "status": "succeeded",
                    "result": {
                        "planId": plan_id,
                        "planStatus": updated_plan.get("status"),
                        "stepResult": result,
                    },
                    "completed_at": completed_at,
                    "updated_at": completed_at,
                }
            },
        )
    except Exception as exc:
        completed_at = datetime.now(timezone.utc)
        db["patchy_proposals"].update_one(
            {"_id": proposal["_id"]},
            {
                "$set": {
                    "status": "failed",
                    "result": {"error": str(exc)},
                    "completed_at": completed_at,
                    "updated_at": completed_at,
                }
            },
        )
        raise

    return serialize_mongo_doc(db["patchy_proposals"].find_one({"_id": proposal["_id"]}))
