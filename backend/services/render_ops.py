import asyncio
import os
from typing import Any

import httpx

from backend.utils.app_utils import serialize_mongo_doc


_RENDER_API_BASE = "https://api.render.com/v1"
_SERVICE_ENV_KEYS = {
    "bty": "ERRAGENT_BTY_RENDER_SERVICE_ID",
    "saapp": "ERRAGENT_SAAPP_RENDER_SERVICE_ID",
}


class RenderOpsError(ValueError):
    pass


def _service_aliases(target: str) -> list[str]:
    normalized = target.lower()
    if normalized == "all":
        return list(_SERVICE_ENV_KEYS)
    if normalized not in _SERVICE_ENV_KEYS:
        raise RenderOpsError("Usage: render status [all|bty|saapp]")
    return [normalized]


def _configured_service_ids(aliases: list[str]) -> dict[str, str]:
    return {
        alias: os.getenv(_SERVICE_ENV_KEYS[alias], "").strip()
        for alias in aliases
        if os.getenv(_SERVICE_ENV_KEYS[alias], "").strip()
    }


async def _fetch_render_service(client: httpx.AsyncClient, service_id: str) -> dict[str, Any]:
    service_response = await client.get(f"{_RENDER_API_BASE}/services/{service_id}")
    service_response.raise_for_status()
    deploy_response = await client.get(
        f"{_RENDER_API_BASE}/services/{service_id}/deploys",
        params={"limit": 1},
    )
    deploy_response.raise_for_status()
    deploys = deploy_response.json()
    latest_deploy = deploys[0] if isinstance(deploys, list) and deploys else None
    return {"service": service_response.json(), "latestDeploy": latest_deploy}


async def collect_render_status(target: str) -> dict[str, Any]:
    aliases = _service_aliases(target)
    api_key = os.getenv("RENDER_API_KEY", "").strip()
    service_ids = _configured_service_ids(aliases)
    services: list[dict[str, Any]] = []

    if not api_key:
        return {
            "provider": "render",
            "target": target.lower(),
            "status": "not_configured",
            "services": [
                {"alias": alias, "status": "not_configured", "reason": "RENDER_API_KEY is not configured"}
                for alias in aliases
            ],
        }

    missing = [alias for alias in aliases if alias not in service_ids]
    if missing:
        return {
            "provider": "render",
            "target": target.lower(),
            "status": "not_configured",
            "services": [
                {"alias": alias, "status": "not_configured", "reason": f"{_SERVICE_ENV_KEYS[alias]} is not configured"}
                for alias in missing
            ],
        }

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        for alias in aliases:
            try:
                payload = await _fetch_render_service(client, service_ids[alias])
                service = payload["service"]
                deploy = payload["latestDeploy"] or {}
                services.append({
                    "alias": alias,
                    "serviceId": service_ids[alias],
                    "status": "ok",
                    "service": {
                        "name": service.get("name"),
                        "type": service.get("type"),
                        "suspended": service.get("suspended"),
                        "updatedAt": service.get("updatedAt"),
                    },
                    "latestDeploy": {
                        "id": deploy.get("id"),
                        "status": deploy.get("status"),
                        "commit": (deploy.get("commit") or {}).get("id"),
                        "finishedAt": deploy.get("finishedAt"),
                        "createdAt": deploy.get("createdAt"),
                    },
                })
            except httpx.HTTPStatusError as exc:
                services.append({
                    "alias": alias,
                    "serviceId": service_ids[alias],
                    "status": "error",
                    "reason": f"Render API returned HTTP {exc.response.status_code}",
                })
            except (httpx.HTTPError, ValueError) as exc:
                services.append({
                    "alias": alias,
                    "serviceId": service_ids[alias],
                    "status": "error",
                    "reason": f"Render API request failed: {exc}",
                })

    overall = "ok" if all(item["status"] == "ok" for item in services) else "error"
    return serialize_mongo_doc({
        "provider": "render",
        "target": target.lower(),
        "status": overall,
        "services": services,
    })


def format_render_status(report: dict[str, Any]) -> list[str]:
    lines = [f"Render status: {report['status'].upper()}"]
    for item in report["services"]:
        if item["status"] != "ok":
            lines.append(f"{item['alias']}: {item['status'].upper()} | {item['reason']}")
            continue
        service = item["service"]
        deploy = item["latestDeploy"]
        lines.extend([
            f"{item['alias']}: {service.get('name', 'unknown')} | suspended={service.get('suspended')}",
            f"  Latest deploy: {deploy.get('status', 'unknown')} | commit={deploy.get('commit') or 'n/a'}",
            f"  Deploy ID: {deploy.get('id') or 'n/a'} | finished={deploy.get('finishedAt') or 'n/a'}",
        ])
    return lines
