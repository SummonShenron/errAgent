import os
from typing import Any

from google import genai
from google.genai import types

from backend.app.models.patchy_models import PatchyIncidentSynthesis
from backend.prompts.constraints import PATCHY_EVIDENCE_SYNTHESIS_PROMPT
from backend.services.log_broker import LogBroker
from backend.services.production_ops import collect_production_status
from backend.utils.app_utils import serialize_mongo_doc


_ALLOWED_ACTIONS = {
    "explain",
    "logs all error",
    "logs bty error",
    "logs saapp error",
    "diagnostics",
    "ops status all",
    "ops status bty",
    "ops status saapp",
    "none",
}


class PatchyReasoningError(ValueError):
    pass


def _action_is_allowed(command: str) -> bool:
    normalized = " ".join(command.strip().lower().split())
    return normalized in _ALLOWED_ACTIONS or normalized.startswith("explain ") and len(normalized.split()) == 2


async def synthesize_incident(incident_id: str, db, broker: LogBroker) -> dict[str, Any]:
    incident = db["incidents"].find_one({"_id": incident_id})
    if not incident:
        raise PatchyReasoningError(f"Incident not found: {incident_id}")

    analysis = db["analyses"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    service = "bty" if "bty" in str(incident.get("service_name", "")).lower() else "saapp" if "saapp" in str(incident.get("service_name", "")).lower() else "all"
    logs = await broker.get_history(service=None if service == "all" else service.upper(), level="error", limit=20)
    production = await collect_production_status(service, db, broker)
    evidence = serialize_mongo_doc({
        "incident": incident,
        "analysis": analysis,
        "remediation": remediation,
        "recentErrorLogs": logs,
        "productionStatus": production,
    })

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise PatchyReasoningError("GOOGLE_API_KEY is not configured for Patchy reasoning")

    client = genai.Client(api_key=api_key)
    prompt = PATCHY_EVIDENCE_SYNTHESIS_PROMPT.format(evidence=evidence)
    response = client.models.generate_content(
        model=os.getenv("PATCHY_REASONING_MODEL", "gemini-3.5-flash"),
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PatchyIncidentSynthesis,
            temperature=0.1,
        ),
    )
    synthesis = response.parsed
    if not isinstance(synthesis, PatchyIncidentSynthesis):
        raise PatchyReasoningError("Patchy reasoning returned no valid structured synthesis")
    if not _action_is_allowed(synthesis.recommended_command):
        raise PatchyReasoningError("Patchy reasoning returned an unsupported read-only action")

    return {
        "incidentId": incident_id,
        "synthesis": synthesis.model_dump(),
        "evidence": {
            "errorLogCount": len(logs),
            "productionStatus": production,
        },
    }
