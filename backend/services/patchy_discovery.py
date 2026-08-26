# patchy_discovery.py (new file or existing utilities)

from urllib.parse import urlparse
# patchy_discovery.py
import httpx
from backend.services.patchy_errors import PatchyCommandError
from backend.utils.app_utils import SERVICES, SERVICE_NAME_ALIASES
import logging
from backend.patchy_browser_agent.saapp_runner import run_sonic_discovery_suite

logger = logging.getLogger("errAgent Logger")
# Placeholder functions for discovery methods
async def static_discovery(service: dict) -> list[dict]:
    base = service["url"].rstrip("/")

    candidates = [
        {"method": "GET", "url": f"{base}/", "source": "static", "auth": "public"},
        {"method": "GET", "url": f"{base}/health", "source": "static", "auth": "public"},
        {"method": "GET", "url": f"{base}/status", "source": "static", "auth": "public"},
        {"method": "GET", "url": f"{base}/api/synthetic/capabilities", "source": "static", "auth": "synthetic"},
        {"method": "GET", "url": f"{base}/api/content", "source": "static", "auth": "user"},
        {"method": "GET", "url": f"{base}/admin", "source": "static", "auth": "admin"},
        # add any known BTY/SAAPP-specific paths here
    ]

    return candidates

async def synthetic_discovery(service: dict) -> list[dict]:
    base = service["url"].rstrip("/")
    url = f"{base}/api/synthetic/capabilities"

    endpoints: list[dict] = []

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers={"X-Patchy-Synthetic": "true"})
            if resp.status_code != 200:
                return endpoints

            data = resp.json()
            # expected shape:
            # {
            #   "endpoints": [
            #       {"method": "GET", "path": "/api/consultations", "auth": "public"},
            #       ...
            #   ]
            # }
            for ep in data.get("endpoints", []):
                endpoints.append({
                    "method": ep.get("method", "GET"),
                    "url": f"{base}{ep.get('path')}",
                    "auth": ep.get("auth", "unknown"),
                    "source": "synthetic",
                })

    except Exception as err:
        # log but don't fail discovery
        logger.warning(f"[discover] Synthetic discovery failed for {service['name']}: {err}")

    return endpoints

async def browser_discovery(service: dict, broker) -> list[dict]:
    """
    Uses Sonic to visit the site, click around, and return discovered endpoints.
    """
    try:
        # You implement run_sonic_discovery_suite to:
        # - load base URL
        # - collect <a href>, form actions, fetch/XHR URLs
        # - return a list of {method, url, auth}
        endpoints = await run_sonic_discovery_suite(service["url"])
        for ep in endpoints:
            ep["source"] = "browser"
        return endpoints
    except Exception as err:
        logger.warning(f"[discover] Browser discovery failed for {service['name']}: {err}")
        return []

def _dedupe_and_classify(endpoints: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for ep in endpoints:
        key = (ep.get("method"), ep.get("path"))
        if key not in seen:
            seen.add(key)
            result.append(ep)
    return result

async def _resolve_service_from_target(db, target: str) -> dict:
    # alias case: 'bty', 'saapp', 'sonic'
    alias = target.lower()
    if alias in SERVICE_NAME_ALIASES:
        name = SERVICE_NAME_ALIASES[alias]
        for svc in SERVICES:
            if svc.get("name") == name:
                return svc

    # URL case
    parsed = urlparse(target)
    if parsed.scheme and parsed.netloc:
        # try to match by URL
        for svc in SERVICES:
            if svc.get("url") and parsed.netloc in svc["url"]:
                return svc
        # fallback: synthetic service object
        return {
            "name": parsed.netloc,
            "url": f"{parsed.scheme}://{parsed.netloc}",
        }

    raise PatchyCommandError(f"Unknown service or URL: {target}")

async def discover_endpoints_command(db, broker, target: str) -> dict:
    """
    target: either 'bty', 'saapp', 'sonic', or a full URL like 'https://btyfitness.app'
    """
    service = await _resolve_service_from_target(db, target)

    static_eps = await static_discovery(service)
    synthetic_eps = await synthetic_discovery(service)
    browser_eps = await browser_discovery(service, broker)

    all_eps = static_eps + synthetic_eps + browser_eps
    merged = _dedupe_and_classify(all_eps)

    return {
        "service": service["name"],
        "baseUrl": service["url"],
        "endpointCount": len(merged),
        "endpoints": merged,
    }

def _dedupe_and_classify(endpoints: list[dict]) -> list[dict]:
    seen = {}
    result = []

    for ep in endpoints:
        key = (ep["method"].upper(), ep["url"])
        if key in seen:
            # merge sources/auth if needed
            existing = seen[key]
            if ep.get("source") and ep["source"] not in existing["sources"]:
                existing["sources"].append(ep["source"])
            if existing.get("auth") == "unknown" and ep.get("auth") != "unknown":
                existing["auth"] = ep["auth"]
            continue

        seen[key] = {
            "method": ep["method"].upper(),
            "url": ep["url"],
            "auth": ep.get("auth", "unknown"),
            "sources": [ep.get("source", "unknown")],
        }
        result.append(seen[key])

    return result
