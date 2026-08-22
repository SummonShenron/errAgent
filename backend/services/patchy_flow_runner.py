"""Reusable synthetic flow plans: multi-step HTTP user journeys with assertions.

A flow plan is an ordered list of structured HTTP actions (GET/POST/PUT/PATCH/DELETE)
plus assertions (status, JSON field presence/value, body substring). Steps can capture
values from responses (cookies, JSON fields) and reuse them later via {{variable}}
templates, enabling flows like signup -> login -> authenticated page load.

Flows execute server-side only after HITL approval, against registered service base
URLs. No arbitrary URLs: every step URL must be a path on the flow's registered base.
Flows that need authentication declare an auth block; tokens are resolved from
errAgent environment variables at execution time and never stored in the flow document.
"""

import os
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from backend.utils.app_utils import SERVICES, serialize_mongo_doc


class PatchyFlowError(ValueError):
    pass


_SERVICE_ALIASES = {
    "bty": "BTY Fitness",
    "btyapp": "BTY Fitness",
    "saapp": "SAAPP Widget",
}

_ACTION_TYPES = {"GET", "POST", "PUT", "PATCH", "DELETE", "assert_status", "assert_json", "assert_body"}
_TEMPLATE_PATTERN = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")
_MAX_STEPS = 12
_MAX_BODY_BYTES = 4000
_MAX_FLOWS_PER_SERVICE = 25

_AUTH_TYPES = {"none", "env_bearer", "clerk_session_token"}


def _validate_auth(auth: Any) -> dict[str, Any]:
    """Validate a flow-level auth block. Tokens never live in the flow document."""
    if auth is None:
        return {"type": "none"}
    if not isinstance(auth, dict):
        raise PatchyFlowError("auth must be an object like {\"type\":\"env_bearer\",\"env\":\"ERRAGENT_BTY_ADMIN_TOKEN\"}")
    auth_type = str(auth.get("type") or "").strip()
    if auth_type not in _AUTH_TYPES:
        raise PatchyFlowError(f"auth type must be one of: {', '.join(sorted(_AUTH_TYPES))}")
    if auth_type == "env_bearer":
        env_name = str(auth.get("env") or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            raise PatchyFlowError("env_bearer auth requires an UPPER_SNAKE env var name")
        if not env_name.startswith("ERRAGENT_"):
            raise PatchyFlowError("env_bearer auth can only read ERRAGENT_* environment variables")
        return {"type": "env_bearer", "env": env_name}
    if auth_type == "clerk_session_token":
        raise PatchyFlowError(
            "clerk_session_token auth is not enabled yet. Create a synthetic admin in Clerk, "
            "mint a long-lived session token, and use env_bearer with an ERRAGENT_* variable instead."
        )
    return {"type": "none"}


def _resolve_auth_headers(auth: dict[str, Any]) -> dict[str, str]:
    """Resolve the auth block to request headers at execution time."""
    if auth.get("type") == "env_bearer":
        token = os.getenv(auth["env"], "").strip()
        if not token:
            raise PatchyFlowError(
                f"Auth token is not configured. Set {auth['env']} in the errAgent backend environment "
                "(a Clerk session token for the synthetic admin user)."
            )
        return {"Authorization": f"Bearer {token}"}
    return {}


def _service_base_url(alias: str) -> tuple[str, str]:
    service_name = _SERVICE_ALIASES.get(alias.lower())
    if not service_name:
        raise PatchyFlowError(f"Unknown service alias: {alias}. Known: {', '.join(sorted(_SERVICE_ALIASES))}")
    service = next((item for item in SERVICES if item["name"] == service_name), None)
    if not service:
        raise PatchyFlowError(f"Service is not registered: {service_name}")
    return service_name, service["url"].rstrip("/")


def _validate_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise PatchyFlowError(f"Step {index + 1} must be an object")
    step_type = str(step.get("type") or "").strip()
    if step_type not in _ACTION_TYPES:
        raise PatchyFlowError(f"Step {index + 1} has unsupported type: {step_type or 'missing'}")

    validated: dict[str, Any] = {"type": step_type}

    if step_type in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        url = str(step.get("url") or "").strip()
        if not url.startswith("/") or url.startswith("//") or ".." in url:
            raise PatchyFlowError(f"Step {index + 1} URL must be a site-relative path like /login")
        validated["url"] = url
        if step_type != "GET":
            body = step.get("body")
            if body is not None:
                if not isinstance(body, dict):
                    raise PatchyFlowError(f"Step {index + 1} body must be a JSON object")
                if len(str(body)) > _MAX_BODY_BYTES:
                    raise PatchyFlowError(f"Step {index + 1} body is too large")
                validated["body"] = body
        headers = step.get("headers")
        if headers is not None:
            if not isinstance(headers, dict) or len(headers) > 10:
                raise PatchyFlowError(f"Step {index + 1} headers must be a small object")
            validated["headers"] = {str(k): str(v) for k, v in headers.items()}
        expect = step.get("expect_status")
        if expect is not None:
            if not isinstance(expect, int) or not 100 <= expect <= 599:
                raise PatchyFlowError(f"Step {index + 1} expect_status must be an HTTP status code")
            validated["expect_status"] = expect
        capture = step.get("capture")
        if capture is not None:
            if not isinstance(capture, dict):
                raise PatchyFlowError(f"Step {index + 1} capture must be an object")
            clean_capture = {}
            for name, source in capture.items():
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", str(name)):
                    raise PatchyFlowError(f"Step {index + 1} capture name is invalid: {name}")
                source_str = str(source)
                if not re.fullmatch(r"(json|header|cookie)\.[a-zA-Z0-9_.-]+", source_str):
                    raise PatchyFlowError(
                        f"Step {index + 1} capture source must be json.<field>, header.<name>, or cookie.<name>"
                    )
                clean_capture[str(name)] = source_str
            validated["capture"] = clean_capture
        return validated

    if step_type == "assert_status":
        equals = step.get("equals")
        if not isinstance(equals, int) or not 100 <= equals <= 599:
            raise PatchyFlowError(f"Step {index + 1} assert_status requires an integer HTTP status")
        validated["equals"] = equals
        return validated

    if step_type == "assert_json":
        has = step.get("has")
        equals = step.get("equals")
        if has is None and equals is None:
            raise PatchyFlowError(f"Step {index + 1} assert_json requires 'has' (field) or 'equals' (field:value)")
        if has is not None:
            validated["has"] = str(has)
        if equals is not None:
            if not isinstance(equals, dict) or len(equals) != 1:
                raise PatchyFlowError(f"Step {index + 1} assert_json equals must be one {{field: value}} pair")
            field, value = next(iter(equals.items()))
            validated["equals"] = {str(field): value}
        return validated

    # assert_body
    contains = step.get("contains")
    if not isinstance(contains, str) or not contains.strip():
        raise PatchyFlowError(f"Step {index + 1} assert_body requires a 'contains' string")
    validated["contains"] = contains[:500]
    return validated


def _render_template(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            key = match.group(1)
            if key not in variables:
                raise PatchyFlowError(f"Template variable '{{{{{key}}}}}' was not captured by an earlier step")
            return str(variables[key])
        return _TEMPLATE_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _render_template(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(item, variables) for item in value]
    return value


def create_flow_plan(alias: str, name: str, actions: list[dict[str, Any]], actor: str, db, auth: Any = None) -> dict[str, Any]:
    service_name, base_url = _service_base_url(alias)
    if not isinstance(name, str) or not name.strip():
        raise PatchyFlowError("Flow name is required")
    name = name.strip()[:80]
    if not isinstance(actions, list) or not actions:
        raise PatchyFlowError("A flow needs at least one action")
    if len(actions) > _MAX_STEPS:
        raise PatchyFlowError(f"Flows are limited to {_MAX_STEPS} steps")

    validated_steps = [_validate_step(step, index) for index, step in enumerate(actions)]
    validated_auth = _validate_auth(auth)
    has_request = any(step["type"] in {"GET", "POST", "PUT", "PATCH", "DELETE"} for step in validated_steps)
    if not has_request:
        raise PatchyFlowError("A flow needs at least one HTTP request step")

    existing = db["patchy_flow_plans"].count_documents({"service": service_name})
    if existing >= _MAX_FLOWS_PER_SERVICE:
        raise PatchyFlowError(f"Too many stored flows for {service_name}. Reuse or retire old flows first.")

    now = datetime.now(timezone.utc)
    document = {
        "_id": f"flow_{uuid4().hex}",
        "service": service_name,
        "alias": alias.lower(),
        "name": name,
        "base_url": base_url,
        "steps": validated_steps,
        "auth": validated_auth,
        "status": "ready",
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_flow_plans"].insert_one(document)
    return serialize_mongo_doc(document)


def list_flow_plans(db, alias: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if alias:
        service_name = _SERVICE_ALIASES.get(alias.lower())
        if not service_name:
            raise PatchyFlowError(f"Unknown service alias: {alias}")
        query["service"] = service_name
    flows = list(db["patchy_flow_plans"].find(query).sort("created_at", -1).limit(limit))
    return serialize_mongo_doc(flows)


def get_flow_plan(db, flow_id: str) -> dict[str, Any]:
    flow = db["patchy_flow_plans"].find_one({"_id": flow_id})
    if not flow:
        raise PatchyFlowError(f"Flow not found: {flow_id}")
    return flow


def create_flow_proposal(flow_id: str, actor: str, db) -> dict[str, Any]:
    flow = get_flow_plan(db, flow_id)
    if flow.get("status") not in {"ready", "passed", "failed"}:
        raise PatchyFlowError(f"Flow cannot run from status: {flow.get('status', 'unknown')}")

    now = datetime.now(timezone.utc)
    proposal_id = f"flow_run_{flow_id}_{int(now.timestamp())}"
    request_steps = [step for step in flow["steps"] if step["type"] in {"GET", "POST", "PUT", "PATCH", "DELETE"}]
    auth_label = (flow.get("auth") or {}).get("type", "none")
    proposal = {
        "_id": proposal_id,
        "kind": "synthetic_flow",
        "risk": "registered_service_flow",
        "status": "awaiting_approval",
        "summary": f"Run flow '{flow['name']}' ({len(request_steps)} request(s), auth: {auth_label}) against {flow['service']}",
        "action": {
            "method": "FLOW",
            "url": flow["base_url"],
            "timeoutSeconds": 15,
            "flowId": flow_id,
            "stepCount": len(flow["steps"]),
        },
        "flowId": flow_id,
        "service": flow["service"],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_flow_runs"].update_one(
        {"_id": proposal_id},
        {"$setOnInsert": {"_id": proposal_id, "flowId": flow_id, "status": "awaiting_approval", "created_at": now}},
        upsert=True,
    )
    db["patchy_proposals"].insert_one(proposal)
    db["patchy_flow_plans"].update_one(
        {"_id": flow_id},
        {"$set": {"status": "awaiting_approval", "proposal_id": proposal_id, "updated_at": now}},
    )
    return serialize_mongo_doc(proposal)


def _extract_capture(source: str, response: httpx.Response) -> Any:
    kind, _, key = source.partition(".")
    if kind == "json":
        try:
            data = response.json()
        except Exception as exc:
            raise PatchyFlowError(f"Capture failed: response was not JSON ({source})") from exc
        value: Any = data
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise PatchyFlowError(f"Capture failed: JSON field '{key}' not present ({source})")
            value = value[part]
        return value
    if kind == "header":
        value = response.headers.get(key)
        if value is None:
            raise PatchyFlowError(f"Capture failed: header '{key}' not present ({source})")
        return value
    if kind == "cookie":
        value = response.cookies.get(key)
        if value is None:
            raise PatchyFlowError(f"Capture failed: cookie '{key}' not present ({source})")
        return value
    raise PatchyFlowError(f"Unsupported capture source: {source}")


async def execute_flow(db, proposal_id: str, actor: str) -> dict[str, Any]:
    proposal = db["patchy_proposals"].find_one({"_id": proposal_id})
    if not proposal or proposal.get("kind") != "synthetic_flow":
        raise PatchyFlowError("Flow proposal not found")
    if proposal.get("status") != "awaiting_approval":
        raise PatchyFlowError(f"Proposal cannot be approved from status: {proposal.get('status', 'unknown')}")

    flow = get_flow_plan(db, proposal["flowId"])
    started_at = datetime.now(timezone.utc)
    claimed = db["patchy_proposals"].update_one(
        {"_id": proposal_id, "status": "awaiting_approval"},
        {"$set": {"status": "running", "approved_by": actor, "approved_at": started_at, "updated_at": started_at}},
    )
    if claimed.modified_count != 1:
        raise PatchyFlowError("Proposal was already claimed")

    step_results: list[dict[str, Any]] = []
    variables: dict[str, Any] = {}
    last_response: httpx.Response | None = None
    failure: str | None = None
    base_url = flow["base_url"]

    try:
        auth_headers = _resolve_auth_headers(flow.get("auth") or {"type": "none"})
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            for index, step in enumerate(flow["steps"]):
                step_type = step["type"]
                rendered = _render_template(step, variables)

                if step_type in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    url = f"{base_url}{rendered['url']}"
                    merged_headers = {**auth_headers, **(rendered.get("headers") or {})}
                    response = await client.request(
                        step_type,
                        url,
                        json=rendered.get("body"),
                        headers=merged_headers or None,
                    )
                    last_response = response
                    expected = rendered.get("expect_status", 200)
                    ok = response.status_code == expected if "expect_status" in rendered else 200 <= response.status_code < 400
                    result: dict[str, Any] = {
                        "index": index,
                        "type": step_type,
                        "url": rendered["url"],
                        "status": "passed" if ok else "failed",
                        "httpStatus": response.status_code,
                        "elapsedMs": round(response.elapsed.total_seconds() * 1000),
                    }
                    if not ok:
                        result["detail"] = f"Expected HTTP {expected}, got {response.status_code}"
                        step_results.append(result)
                        failure = f"Step {index + 1} ({step_type} {rendered['url']}): expected {expected}, got {response.status_code}"
                        break
                    for var_name, source in (rendered.get("capture") or {}).items():
                        variables[var_name] = _extract_capture(source, response)
                    step_results.append(result)

                elif step_type == "assert_status":
                    actual = last_response.status_code if last_response is not None else None
                    ok = actual == rendered["equals"]
                    step_results.append({
                        "index": index,
                        "type": step_type,
                        "status": "passed" if ok else "failed",
                        "expected": rendered["equals"],
                        "actual": actual,
                    })
                    if not ok:
                        failure = f"Step {index + 1} (assert_status): expected {rendered['equals']}, got {actual}"
                        break

                elif step_type == "assert_json":
                    if last_response is None:
                        failure = f"Step {index + 1} (assert_json): no preceding response"
                        step_results.append({"index": index, "type": step_type, "status": "failed", "detail": failure})
                        break
                    try:
                        data = last_response.json()
                    except Exception:
                        failure = f"Step {index + 1} (assert_json): response was not JSON"
                        step_results.append({"index": index, "type": step_type, "status": "failed", "detail": failure})
                        break
                    ok = True
                    detail = None
                    if "has" in rendered:
                        value: Any = data
                        for part in str(rendered["has"]).split("."):
                            if not isinstance(value, dict) or part not in value:
                                ok = False
                                detail = f"missing JSON field '{rendered['has']}'"
                                break
                            value = value[part]
                    if ok and "equals" in rendered:
                        field, expected_value = next(iter(rendered["equals"].items()))
                        value = data
                        for part in field.split("."):
                            value = value.get(part) if isinstance(value, dict) else None
                        if value != expected_value:
                            ok = False
                            detail = f"JSON field '{field}' was {value!r}, expected {expected_value!r}"
                    step_results.append({"index": index, "type": step_type, "status": "passed" if ok else "failed", "detail": detail})
                    if not ok:
                        failure = f"Step {index + 1} (assert_json): {detail}"
                        break

                else:  # assert_body
                    if last_response is None:
                        failure = f"Step {index + 1} (assert_body): no preceding response"
                        step_results.append({"index": index, "type": step_type, "status": "failed", "detail": failure})
                        break
                    ok = rendered["contains"] in last_response.text
                    step_results.append({"index": index, "type": step_type, "status": "passed" if ok else "failed"})
                    if not ok:
                        failure = f"Step {index + 1} (assert_body): response did not contain '{rendered['contains'][:80]}'"
                        break
    except PatchyFlowError as exc:
        failure = str(exc)
    except httpx.HTTPError as exc:
        failure = f"Network error: {exc}"

    completed_at = datetime.now(timezone.utc)
    final_status = "failed" if failure else "succeeded"
    result_doc = {
        "flowId": flow["_id"],
        "flowName": flow["name"],
        "service": flow["service"],
        "steps": step_results,
        "stepsPassed": len([s for s in step_results if s.get("status") == "passed"]),
        "stepsTotal": len(flow["steps"]),
        "failure": failure,
    }
    db["patchy_proposals"].update_one(
        {"_id": proposal_id},
        {"$set": {"status": final_status, "result": result_doc, "completed_at": completed_at, "updated_at": completed_at}},
    )
    db["patchy_flow_plans"].update_one(
        {"_id": flow["_id"]},
        {"$set": {
            "status": "passed" if final_status == "succeeded" else "failed",
            "last_run_at": completed_at,
            "updated_at": completed_at,
        }},
    )
    return serialize_mongo_doc(db["patchy_proposals"].find_one({"_id": proposal_id}))
