# backend/patchy_browser_agent/saapp_runner.py
import logging
import asyncio
from backend.patchy_browser_agent.browserless_client import PlaywrightBrowserlessClient
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


# backend/patchy_browser_agent/saapp_runner.py

async def run_sonic_discovery_suite(base_url: str) -> list[dict]:
    endpoints = []
    client = PlaywrightBrowserlessClient()

    try:
        logger.info(f"[sonic-discovery] Navigating to target: {base_url}")
        await client.connect()
        await client.goto(base_url)

        # Allow React/Vite DOM hydration & background fetch requests to complete
        if client.page:
            await client.page.wait_for_timeout(2500)

        # 1. Extract <a href> links
        links = await client.eval("""
            Array.from(document.querySelectorAll('a[href]'))
                 .map(a => a.href)
        """) or []
        logger.info(f"[sonic-discovery] Discovered {len(links)} HTML links")
        
        for href in links:
            endpoints.append({
                "method": "GET",
                "url": href,
                "auth": "unknown",
                "source": "browser-link"
            })

        # 2. Extract <form action> URLs
        forms = await client.eval("""
            Array.from(document.querySelectorAll('form[action]'))
                 .map(f => f.action)
        """) or []
        logger.info(f"[sonic-discovery] Discovered {len(forms)} HTML forms")

        for action in forms:
            endpoints.append({
                "method": "POST",
                "url": action,
                "auth": "unknown",
                "source": "browser-form"
            })

        # 3. Extract captured network events
        network_events = await client.get_network_events()
        logger.info(f"[sonic-discovery] Captured {len(network_events)} network requests")

        for evt in network_events:
            req = evt.get("request", {})
            url = req.get("url")
            method = req.get("method", "GET")

            if url:
                endpoints.append({
                    "method": method,
                    "url": url,
                    "auth": "unknown",
                    "source": "browser-network"
                })

    except Exception as err:
        logger.error(f"[sonic-discovery] Failed: {err}")

    finally:
        await client.close()

    logger.info(f"[sonic-discovery] Total endpoints discovered: {len(endpoints)}")
    return endpoints


if __name__ == "__main__":
    asyncio.run(run_sonic_security_suite())
    