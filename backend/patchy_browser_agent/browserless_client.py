import asyncio
import os
import json
import logging
import websockets
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("errAgent Logger")

BROWSERLESS_WS = f"wss://playwright.browserless.io/playwright?token={os.getenv('BROWSERLESS_TOKEN')}"
class BrowserlessClient:
    def __init__(self, ws_url=BROWSERLESS_WS):
        self.ws_url = ws_url
        self.websocket = None
        self.session_id = None
        self.page_id = None

    async def connect(self):
        self.websocket = await websockets.connect(self.ws_url)
        logger.info("[browserless] Connected to Browserless")

        # Create Playwright session
        await self._send({"id": 1, "method": "Playwright.enable"})
        self.session_id = 1

        # Create browser
        await self._send({
            "id": 2,
            "method": "Playwright.createBrowser",
            "params": {"browserType": "chromium"}
        })

        # Create context
        await self._send({
            "id": 3,
            "method": "Browser.createContext",
            "params": {"browser": 2}
        })

        # Create page
        await self._send({
            "id": 4,
            "method": "BrowserContext.newPage",
            "params": {"context": 3}
        })

        self.page_id = 4
        logger.info("[browserless] Browser, context, and page created")

    async def _send(self, payload):
        await self.websocket.send(json.dumps(payload))
        response = await self.websocket.recv()
        return json.loads(response)

    async def goto(self, url: str):
        await self._send({
            "id": 10,
            "method": "Page.navigate",
            "params": {"page": self.page_id, "url": url}
        })

    async def eval(self, expression: str):
        result = await self._send({
            "id": 11,
            "method": "Page.evaluate",
            "params": {"page": self.page_id, "expression": expression}
        })
        return result.get("result")

    async def get_network_events(self):
        """Collect network events from Browserless."""
        events = []

        async for msg in self.websocket:
            data = json.loads(msg)
            if "method" in data and data["method"] == "Network.requestWillBeSent":
                events.append(data["params"])
            if len(events) > 200:
                break

        return events

    async def close(self):
        if self.websocket:
            await self.websocket.close()
            logger.info("[browserless] Connection closed")
