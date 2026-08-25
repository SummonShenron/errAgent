import os
import logging
import httpx

logger = logging.getLogger("errAgent Logger")

CLERK_API_BASE = os.getenv("CLERK_API_BASE", "https://api.clerk.com/v1")
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY")

SONIC_ADMIN_USER_ID = os.getenv("SONIC_ADMIN_USER_ID")      # Clerk user ID for admin
SONIC_USER_USER_ID = os.getenv("SONIC_USER_USER_ID")        # Clerk user ID for non-admin user


async def _create_session(user_id: str) -> str:
    """Create a Clerk session for a given user_id and return session_id."""
    if not CLERK_SECRET_KEY:
        raise RuntimeError("CLERK_SECRET_KEY is not set")

    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}
    async with httpx.AsyncClient(base_url=CLERK_API_BASE, timeout=15.0) as client:
        res = await client.post(
            "/sessions",
            headers=headers,
            json={"user_id": user_id},
        )
        res.raise_for_status()
        data = res.json()
        session_id = data.get("id")
        logger.info(f"[sonic-clerk] Created session {session_id} for user {user_id}")
        return session_id


async def _mint_session_token(session_id: str) -> str:
    """Mint a JWT for a given Clerk session_id."""
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}
    async with httpx.AsyncClient(base_url=CLERK_API_BASE, timeout=15.0) as client:
        res = await client.post(
            f"/sessions/{session_id}/tokens",
            headers=headers,
            json={"jwt": {}},
        )
        res.raise_for_status()
        data = res.json()
        token = data.get("jwt")
        logger.info(f"[sonic-clerk] Minted JWT for session {session_id} (len={len(token) if token else 0})")
        return token


async def get_user_jwt() -> str:
    """Non-admin Sonic user JWT."""
    if not SONIC_USER_USER_ID:
        raise RuntimeError("SONIC_USER_USER_ID is not set")
    session_id = await _create_session(SONIC_USER_USER_ID)
    return await _mint_session_token(session_id)


async def get_admin_jwt() -> str:
    """Admin Sonic user JWT."""
    if not SONIC_ADMIN_USER_ID:
        raise RuntimeError("SONIC_ADMIN_USER_ID is not set")
    session_id = await _create_session(SONIC_ADMIN_USER_ID)
    return await _mint_session_token(session_id)
