import json
import os
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


CircuitOperation = Literal[
    "plan_workflow",
    "patch_workflow",
    "debug_run",
    "generate_connector",
    "job_search_flow",
]


class CircuitArchitectRequest(BaseModel):
    operation: CircuitOperation
    goal: str = Field(min_length=1, max_length=4000)
    incident_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class CircuitArchitectResponse(BaseModel):
    status: Literal["answer", "proposal", "error"]
    summary: str
    findings: list[dict[str, Any]] = Field(default_factory=list)
    patch: dict[str, Any] | None = None
    requires_approval: bool = True
    errors: list[str] = Field(default_factory=list)


class CircuitArchitectError(ValueError):
    pass


_OPERATIONS = {
    "plan_workflow",
    "patch_workflow",
    "debug_run",
    "generate_connector",
    "job_search_flow",
}
_PATCH_KEYS = {
    "plan_workflow": {"add_inputs", "add_nodes", "add_edges"},
    "patch_workflow": {"update_nodes", "add_nodes", "add_edges"},
    "debug_run": {"update_nodes", "add_nodes", "add_edges"},
    "job_search_flow": {"add_inputs", "add_nodes", "add_edges"},
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _validate_patch(operation: str, patch: Any, available_node_types: Any = None) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise CircuitArchitectError("The generated patch must be an object")

    if any(key in patch for key in ("remove_nodes", "remove_edges")):
        raise CircuitArchitectError("Generated patches may not remove nodes or edges")

    unexpected = set(patch) - _PATCH_KEYS[operation]
    if unexpected:
        raise CircuitArchitectError(f"Patch contains unsupported keys: {', '.join(sorted(unexpected))}")

    for key in _PATCH_KEYS[operation]:
        if key in patch and not isinstance(patch[key], list):
            raise CircuitArchitectError(f"Patch field {key} must be a list")

    if operation == "plan_workflow":
        missing = _PATCH_KEYS[operation] - set(patch)
        if missing:
            raise CircuitArchitectError(f"Complete workflow patch is missing: {', '.join(sorted(missing))}")

    if available_node_types:
        allowed = {str(node_type) for node_type in available_node_types}
        for node in patch.get("add_nodes", []):
            if not isinstance(node, dict):
                raise CircuitArchitectError("Every added node must be an object")
            node_type = node.get("type", node.get("node_type"))
            if node_type not in allowed:
                raise CircuitArchitectError(f"Node type is not available: {node_type}")

    return patch


def _build_evidence(request: CircuitArchitectRequest, incident: dict[str, Any] | None) -> dict[str, Any]:
    context = _json_safe(request.context)
    evidence = {
        "operation": request.operation,
        "goal": request.goal,
        "context": context,
    }
    if incident is not None:
        evidence["incident"] = _json_safe(incident)
    if request.operation == "debug_run":
        latest_run = context.get("latest_run", {}) if isinstance(context, dict) else {}
        evidence["debug_evidence"] = {
            key: latest_run.get(key, [])
            for key in ("errors", "logs", "trace", "outputs")
        } if isinstance(latest_run, dict) else {}
    return evidence


def _operation_rules(operation: str) -> str:
    rules = {
        "debug_run": "Return root cause, evidence, failed node, suggested fix, and an optional safe WorkflowPatch.",
        "plan_workflow": "Return a complete graph patch with add_inputs, add_nodes, and add_edges. Use only available node types.",
        "patch_workflow": "Return only the smallest update_nodes, add_nodes, and add_edges changes. Never remove nodes or edges.",
        "generate_connector": "Return a connector proposal with name, config_schema, output_schema, request_definition, and security_notes; do not return executable code.",
        "job_search_flow": "Return a complete proposed workflow containing job search/API, transform merge, LLM filter/ranker, Google Sheets, and Resend email nodes.",
    }
    return rules[operation]


async def create_circuit_architect_response(
    request: CircuitArchitectRequest,
    db: Any = None,
) -> CircuitArchitectResponse:
    if request.operation not in _OPERATIONS:
        return CircuitArchitectResponse(status="error", summary="Unsupported circuit architect operation", errors=[request.operation])

    incident = None
    if request.incident_id:
        if db is None:
            return CircuitArchitectResponse(status="error", summary="Incident data is unavailable", errors=["Database is not configured"])
        incident = db["incidents"].find_one({"_id": request.incident_id})
        if not incident:
            return CircuitArchitectResponse(status="error", summary="Incident was not found", errors=[request.incident_id])
        incident = {
            "incident": incident,
            "analysis": db["analyses"].find_one(
                {"incident_id": request.incident_id},
                sort=[("updated_at", -1), ("created_at", -1)],
            ) or {},
            "remediation": db["remediations"].find_one(
                {"incident_id": request.incident_id},
                sort=[("updated_at", -1), ("created_at", -1)],
            ) or {},
        }

    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return CircuitArchitectResponse(status="error", summary="Gemini is not configured", errors=["GOOGLE_API_KEY is not configured"])

    evidence = _build_evidence(request, incident)
    prompt = (
        "You are ErrAgent's Circuit Architect. Produce a safe, read-only proposal for Circuit. "
        "Do not claim to apply changes, access unavailable data, or execute workflows. "
        f"Operation rules: {_operation_rules(request.operation)}\n"
        "Return JSON matching the supplied response schema. Keep findings evidence-based. "
        f"Evidence:\n{json.dumps(evidence, default=str)}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=os.getenv("CIRCUIT_ARCHITECT_MODEL") or os.getenv("PATCHY_FAST_MODEL", "gemini-3.5-flash"),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CircuitArchitectResponse,
                temperature=0.1,
            ),
        )
        result = CircuitArchitectResponse.model_validate(response.parsed)
        if result.patch is not None and request.operation != "generate_connector":
            result.patch = _validate_patch(
                request.operation,
                result.patch,
                request.context.get("available_node_types"),
            )
        return result
    except CircuitArchitectError as exc:
        return CircuitArchitectResponse(status="error", summary="Generated proposal failed safety validation", errors=[str(exc)])
    except Exception as exc:
        return CircuitArchitectResponse(status="error", summary="Circuit architect generation failed", errors=[str(exc)[:500]])