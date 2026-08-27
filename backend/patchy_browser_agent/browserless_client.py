# backend/patchy_browser_agent/browserless_client.py
import os
import logging
from playwright.async_api import async_playwright

logger = logging.getLogger("errAgent Logger")

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

        # Root endpoint for connect_over_cdp
        ws_url = f"wss://chrome.browserless.io?token={token}"
        self._playwright = await async_playwright().start()
        
        # Connect via CDP
        self.browser = await self._playwright.chromium.connect_over_cdp(ws_url)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

        self.page.on("request", lambda req: self.network_events.append({
            "request": {"url": req.url, "method": req.method}
        }))
        logger.info("[browserless] Connected via Playwright CDP")

    async def goto(self, url: str):
        if not self.page:
            raise RuntimeError("Browser page is not initialized")
        await self.page.goto(url)

    async def eval(self, script: str):
        if not self.page:
            raise RuntimeError("Browser page is not initialized")
        return await self.page.evaluate(script)

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