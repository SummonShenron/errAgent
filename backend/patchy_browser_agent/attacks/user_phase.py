import logging
import httpx

from .endpoints import SONIC_BASE_URL
from .clerk_auth import get_user_jwt

logger = logging.getLogger("errAgent Logger")


async def run_user_phase():
    """Authenticated non-admin user fuzzing."""
    vulns = []

    user_jwt = await get_user_jwt()
    headers = {
        "Authorization": f"Bearer {user_jwt}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(base_url=SONIC_BASE_URL, timeout=30.0) as client:
        # RAG discovery
        logger.info("[sonic-user] Testing GET /api/discover-docs as user...")
        discover_res = await client.get("/api/discover-docs", headers=headers, params={"affiliate": "All"})
        logger.info(f"[sonic-user] /api/discover-docs -> {discover_res.status_code}")

        # Document list
        logger.info("[sonic-user] Testing GET /api/documents as user...")
        docs_res = await client.get("/api/documents", headers=headers, params={"affiliate": "All"})
        logger.info(f"[sonic-user] /api/documents -> {docs_res.status_code}")

        # Document delete
        logger.info("[sonic-user] Testing DELETE /api/documents/{doc_id} as user...")
        doc_del_res = await client.delete(
            "/api/documents/test-doc-id",
            headers=headers,
            params={"affiliate": "All"},
        )
        logger.info(f"[sonic-user] DELETE /api/documents/test-doc-id -> {doc_del_res.status_code}")

        # Chat with attachments
        logger.info("[sonic-user] Testing POST /api/chat with user JWT...")
        chat_payload = {"question": "Test RAG query", "affiliate": "All"}
        chat_res = await client.post("/api/chat", headers=headers, json=chat_payload)
        logger.info(f"[sonic-user] /api/chat -> {chat_res.status_code}")

        # Saved conversations
        logger.info("[sonic-user] Testing GET /api/saved-conversations as user...")
        saved_res = await client.get("/api/saved-conversations", headers=headers)
        logger.info(f"[sonic-user] /api/saved-conversations -> {saved_res.status_code}")

        # Insights
        logger.info("[sonic-user] Testing GET /api/insights as user...")
        insights_res = await client.get("/api/insights", headers=headers)
        logger.info(f"[sonic-user] /api/insights -> {insights_res.status_code}")

        # You can add more detailed checks here (cross-affiliate leakage, etc.)

    return vulns
