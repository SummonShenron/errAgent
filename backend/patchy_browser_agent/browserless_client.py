import asyncio
import json
import websockets
import logging
import os

logger = logging.getLogger("errAgent Logger")
BROWSERLESS_WS = f"wss://chrome.browserless.io/playwright?token={os.getenv('BROWSERLESS_TOKEN')}"
class CDPBrowserlessClient:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.ws = None
        self.session_id = None
        self.target_id = None

    async def connect(self):
        self.ws = await websockets.connect(self.ws_url)
        logger.info("[browserless] Connected")

        await self._send({"id": 1, "method": "Target.setDiscoverTargets", "params": {"discover": True}})
        await self._send({"id": 2, "method": "Network.enable"})
        await self._send({"id": 3, "method": "Runtime.enable"})
        await self._send({"id": 4, "method": "Page.enable"})

        # Create a new tab
        resp = await self._send({
            "id": 5,
            "method": "Target.createTarget",
            "params": {"url": "about:blank"}
        })
        self.target_id = resp["result"]["targetId"]

        # Attach to the tab
        resp = await self._send({
            "id": 6,
            "method": "Target.attachToTarget",
            "params": {"targetId": self.target_id, "flatten": True}
        })
        self.session_id = resp["result"]["sessionId"]

        logger.info("[browserless] CDP session established")

    async def goto(self, url):
        await self._send({
            "id": 7,
            "method": "Page.navigate",
            "params": {"url": url},
            "sessionId": self.session_id
        })

    async def eval(self, js):
        resp = await self._send({
            "id": 8,
            "method": "Runtime.evaluate",
            "params": {"expression": js},
            "sessionId": self.session_id
        })
        return resp["result"]

    async def listen_network(self, limit=200):
        events = []
        async for msg in self.ws:
            data = json.loads(msg)
            if data.get("method") == "Network.requestWillBeSent":
                events.append(data["params"])
                if len(events) >= limit:
                    break
        return events

    async def close(self):
        if self.target_id:
            await self._send({
                "id": 9,
                "method": "Target.closeTarget",
                "params": {"targetId": self.target_id}
            })
        if self.ws:
            await self.ws.close()

    async def _send(self, payload):
        await self.ws.send(json.dumps(payload))
        return json.loads(await self.ws.recv())
