import logging
import asyncio

from backend.patchy_browser_agent.attacks.guest_phase import run_guest_phase
from backend.patchy_browser_agent.attacks.user_phase import run_user_phase
from backend.patchy_browser_agent.attacks.admin_phase import run_admin_phase

logger = logging.getLogger("errAgent Logger")


async def run_sonic_security_suite():
    """Executes Sonic security & logic tests across guest, user, and admin identities."""
    logger.info("[sonic-runner] Starting Sonic security suite...")

    guest_vulns = await run_guest_phase()
    user_vulns = await run_user_phase()
    admin_vulns = await run_admin_phase()

    all_vulns = guest_vulns + user_vulns + admin_vulns

    logger.info(f"[sonic-runner] Sonic suite complete. Total vulnerabilities: {len(all_vulns)}")
    return all_vulns


if __name__ == "__main__":
    asyncio.run(run_sonic_security_suite())
