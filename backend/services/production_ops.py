import asyncio
import os
from typing import Any

from backend.services.log_broker import LogBroker
from backend.utils.app_utils import (
    build_health_report,
    get_service_by_alias,
    load_service_registry,
    run_service_health_checks,
    serialize_mongo_doc,
)


def _deployment_metadata(alias: str) -> dict[str, str]:
    prefix = f"ERRAGENT_{alias.upper()}_"
    return {
        "platform": os.getenv(f"{prefix}PLATFORM", "not configured"),
        "deployment": os.getenv(f"{prefix}DEPLOYMENT_ID", "not configured"),
        "version": os.getenv(f"{prefix}VERSION", "not configured"),
        "region": os.getenv(f"{prefix}REGION", "not configured"),
    }


def _service_aliases(target: str) -> list[str]:
    registered_aliases = [service["short_alias"] for service in load_service_registry()]
    if target == "all":
        return registered_aliases
    if target not in registered_aliases:
        raise ValueError("Usage: ops status [all|<registered-service-alias>]")
    return [target]


async def collect_production_status(target: str, db, broker: LogBroker) -> dict[str, Any]:
    aliases = _service_aliases(target.lower())
    health_results = await asyncio.to_thread(run_service_health_checks)
    health_by_name = {item.get("service"): item for item in health_results}
    incidents = list(db["incidents"].find({}).limit(100))
    active_incidents = [
        incident for incident in incidents
        if incident.get("status") not in {"resolved", "closed"}
    ]

    services = []
    for alias in aliases:
        service_name = get_service_by_alias(alias)["service_name"]
        service_health = health_by_name.get(service_name, {"service": service_name, "status": "unknown"})
        service_incidents = [
            incident for incident in active_incidents
            if alias in str(incident.get("service_name", "")).lower()
        ]
        error_logs = await broker.get_history(service=alias.upper(), level="error", limit=10)
        services.append({
            "alias": alias,
            "service": service_name,
            "health": service_health,
            "activeIncidentCount": len(service_incidents),
            "recentErrorCount": len(error_logs),
            "deployment": _deployment_metadata(alias),
            "incidents": service_incidents,
            "errorLogs": error_logs,
        })

    health_report = build_health_report([item["health"] for item in services])
    overall_status = "healthy"
    if any(item["health"].get("status") == "down" for item in services):
        overall_status = "down"
    elif any(item["activeIncidentCount"] or item["recentErrorCount"] for item in services):
        overall_status = "degraded"

    return serialize_mongo_doc({
        "target": target.lower(),
        "overallStatus": overall_status,
        "health": health_report,
        "services": services,
    })


def format_production_status(report: dict[str, Any]) -> list[str]:
    lines = [f"Overall production status: {report['overallStatus'].upper()}"]
    for service in report["services"]:
        deployment = service["deployment"]
        lines.extend([
            f"{service['service']} ({service['alias']}): {service['health'].get('status', 'unknown').upper()}",
            f"  Active incidents: {service['activeIncidentCount']}",
            f"  Recent error logs: {service['recentErrorCount']}",
            f"  Deployment: {deployment['deployment']} | version: {deployment['version']} | platform: {deployment['platform']}",
        ])
    return lines
