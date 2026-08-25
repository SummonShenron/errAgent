import os
import logging
import httpx

logger = logging.getLogger("errAgent Logger")

CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY")
# Your admin user's ID from the Clerk Dashboard (starts with 'user_...')
CLERK_USER_ID = os.getenv("CLERK_TEST_USER_ID")


async def run_browser_and_get_token() -> str:
    """Mints an authenticated JWT directly from Clerk's Backend API."""
    if not CLERK_SECRET_KEY:
        raise ValueError("[pentest] Missing CLERK_SECRET_KEY environment variable.")
    if not CLERK_USER_ID:
        raise ValueError("[pentest] Missing CLERK_TEST_USER_ID environment variable.")

    headers = {
        "Authorization": f"Bearer {CLERK_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Step 1: Create a valid session for the target user ID
        logger.info(f"[pentest] Creating Clerk session for user {CLERK_USER_ID}...")
        session_res = await client.post(
            "https://api.clerk.com/v1/sessions",
            headers=headers,
            json={"user_id": CLERK_USER_ID}
        )

        if session_res.status_code != 200:
            raise RuntimeError(f"[pentest] Failed to create Clerk session: {session_res.text}")

        session_id = session_res.json().get("id")

        # Step 2: Mint a session JWT token
        logger.info(f"[pentest] Minting session JWT for session {session_id}...")
        token_res = await client.post(
            f"https://api.clerk.com/v1/sessions/{session_id}/tokens",
            headers=headers
        )

        if token_res.status_code != 200:
            raise RuntimeError(f"[pentest] Failed to mint session JWT: {token_res.text}")

        jwt = token_res.json().get("jwt")
        if not jwt or not isinstance(jwt, str):
            raise RuntimeError("[pentest] Clerk API returned an invalid or empty JWT.")

        logger.info(f"[pentest] Successfully generated Clerk JWT (Length: {len(jwt)}).")
        return jwt