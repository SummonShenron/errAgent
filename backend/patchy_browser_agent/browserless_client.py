# backend/patchy_browser_agent/browserless_client.py
import os
import logging
from playwright.async_api import async_playwright

logger = logging.getLogger("errAgent Logger")

class PlaywrightBrowserlessClient:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._playwright = None

    async def connect(self):
        token = os.getenv("BROWSERLESS_TOKEN")
        if not token:
            raise ValueError("BROWSERLESS_TOKEN environment variable is not set")

        ws_url = f"wss://chrome.browserless.io/playwright?token={token}"
        self._playwright = await async_playwright().start()
        
        # Connect over CDP using Playwright's native WebSocket connector
        self.browser = await self._playwright.chromium.connect_over_cdp(ws_url)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        logger.info("[browserless] Playwright browser session connected")

    async def goto(self, url: str):
        if self.page:
            await self.page.goto(url, wait_until="networkidle")

    async def eval(self, js_expression: str):
        if self.page:
            return await self.page.evaluate(js_expression)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()