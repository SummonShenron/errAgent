# backend/patchy_browser_agent/saapp_runner.py
import logging
import asyncio
from backend.patchy_browser_agent.browserless_client import PlaywrightBrowserlessClient
from backend.patchy_browser_agent.attacks.guest_phase import run_guest_phase
from backend.patchy_browser_agent.attacks.user_phase import run_user_phase
from backend.patchy_browser_agent.attacks.admin_phase import run_admin_phase
from backend.patchy_browser_agent.attacks.generic_phase import run_generic_phase

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


async def run_sonic_discovery_suite(base_url: str) -> list[dict]:
    raw_endpoints = []
    client = PlaywrightBrowserlessClient()

    try:
        logger.info(f"[sonic-discovery] Navigating to target: {base_url}")
        await client.connect()

        visited_urls = set()
        queue = [base_url]
        max_pages = 5  # Limits page depth to avoid infinite loops

        while queue and len(visited_urls) < max_pages:
            target_url = queue.pop(0)
            if target_url in visited_urls:
                continue
            
            visited_urls.add(target_url)
            logger.info(f"[sonic-discovery] Scanning route: {target_url}")
            
            try:
                await client.goto(target_url)
                if client.page:
                    await client.page.wait_for_timeout(1500)
            except Exception as nav_err:
                logger.warning(f"[sonic-discovery] Navigation skipped for {target_url}: {nav_err}")
                continue

            # 1. Extract <a href> links on current page
            links = await client.eval("""
                Array.from(document.querySelectorAll('a[href]'))
                     .map(a => a.href)
            """) or []
            
            for href in links:
                raw_endpoints.append({
                    "method": "GET",
                    "url": href,
                    "auth": "unknown",
                    "source": "browser-link"
                })
                # Add internal links to crawl queue
                if href.startswith(base_url) and href not in visited_urls and href not in queue:
                    queue.append(href)

            # 2. Extract <form action> URLs
            forms = await client.eval("""
                Array.from(document.querySelectorAll('form[action]'))
                     .map(f => f.action)
            """) or []
            
            for action in forms:
                raw_endpoints.append({
                    "method": "POST",
                    "url": action,
                    "auth": "unknown",
                    "source": "browser-form"
                })

            # 3. Trigger client-side React UI elements to surface hidden fetch calls
            await client.eval("""
                document.querySelectorAll('button, nav a, [role="button"]').forEach(el => {
                    try { el.click(); } catch (e) {}
                });
            """)
            if client.page:
                await client.page.wait_for_timeout(1000)

        # 4. Extract filtered network events captured across all visited routes
        network_events = await client.get_network_events()
        for evt in network_events:
            req = evt.get("request", {})
            url = req.get("url")
            method = req.get("method", "GET")

            if url:
                raw_endpoints.append({
                    "method": method,
                    "url": url,
                    "auth": "unknown",
                    "source": "browser-network"
                })

    except Exception as err:
        logger.error(f"[sonic-discovery] Failed: {err}")

    finally:
        await client.close()

    # Deduplicate endpoints by (method, url)
    seen = set()
    endpoints = []
    for ep in raw_endpoints:
        key = (ep["method"], ep["url"])
        if key not in seen:
            seen.add(key)
            endpoints.append(ep)

    logger.info(f"[sonic-discovery] Total unique endpoints discovered: {len(endpoints)}")
    return endpoints

async def run_generic_security_suite(target_url: str) -> list[dict]:
    """Runs automated discovery followed by generic security audits on any target site."""
    logger.info(f"[security-runner] Starting generic security suite for {target_url}...")
    
    # Step 1: Discover endpoints
    endpoints = await run_sonic_discovery_suite(target_url)
    
    # Step 2: Run generic attack phase against discovered endpoints
    vulnerabilities = await run_generic_phase(endpoints, target_url)
    
    logger.info(f"[security-runner] Security scan complete. Total vulnerabilities found: {len(vulnerabilities)}")
    return vulnerabilities

if __name__ == "__main__":
    asyncio.run(run_sonic_security_suite())