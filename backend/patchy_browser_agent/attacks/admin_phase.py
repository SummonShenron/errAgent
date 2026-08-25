import logging
import httpx

from .endpoints import SONIC_BASE_URL
from .clerk_auth import get_admin_jwt

logger = logging.getLogger("errAgent Logger")


async def run_admin_phase():
    """Admin-only fuzzing for PAAPP and privileged operations."""
    vulns = []

    admin_jwt = await get_admin_jwt()
    headers = {
        "Authorization": f"Bearer {admin_jwt}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(base_url=SONIC_BASE_URL, timeout=30.0) as client:
        logger.info("[sonic-admin] Testing GET /admin/paapp with admin JWT...")
        paapp_res = await client.get("/admin/paapp", headers=headers)
        logger.info(f"[sonic-admin] /admin/paapp -> {paapp_res.status_code} | Body: {paapp_res.text}")

        if paapp_res.status_code != 200:
            vulns.append({
                "endpoint": "/admin/paapp",
                "type": "ADMIN_ACCESS_FAILURE",
                "severity": "MEDIUM",
                "detail": "Admin JWT could not access PAAPP endpoint as expected.",
            })

        # Here you’d add more specific PAAPP fuzzing once you expose sub-operations via API.

    return vulns
