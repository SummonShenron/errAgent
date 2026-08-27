# backend/patchy_browser_agent/browserless_client.py
import os
import logging
from playwright.async_api import async_playwright

logger = logging.getLogger("errAgent Logger")

IGNORED_PATTERNS = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", 
    ".woff", ".woff2", ".ico", "blob:", "clerk.accounts.dev"
)

class PlaywrightBrowserlessClient:
    def __init__(self):
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.network_events = []

    async def connect(self):
        token = os.getenv("BROWSERLESS_TOKEN")
        if not token:
            raise ValueError("BROWSERLESS_TOKEN environment variable is missing or empty")

        ws_url = f"wss://chrome.browserless.io?token={token}"
        self._playwright = await async_playwright().start()
        
        self.browser = await self._playwright.chromium.connect_over_cdp(ws_url)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

        # Filter out static noise to capture real API calls
        def handle_request(req):
            url = req.url.lower()
            if not any(pattern in url for pattern in IGNORED_PATTERNS):
                self.network_events.append({
                    "request": {"url": req.url, "method": req.method}
                })

        self.page.on("request", handle_request)
        logger.info("[browserless] Connected via Playwright CDP")

    async def goto(self, url: str, wait_until: str = "networkidle"):
        if not self.page:
            raise RuntimeError("Browser page is not initialized")
        await self.page.goto(url, wait_until=wait_until)

    async def eval(self, script: str):
        if not self.page:
            raise RuntimeError("Browser page is not initialized")
        return await self.page.evaluate(script)

    async def capture_network(self, duration_ms: int = 5000):
        if self.page:
            await self.page.wait_for_timeout(duration_ms)
        return self.network_events

    async def get_network_events(self):
        return self.network_events

    async def close(self):
        if self.page:
            await self.page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("[browserless] Closed Playwright connection")