# patchy_browser_agent/token_bridge.py

import httpx

BTY_API = "https://btyapp.onrender.com"

def make_admin_client(token: str):
    return httpx.AsyncClient(
        base_url=BTY_API,
        headers={"Authorization": f"Bearer {token}"}
    )
