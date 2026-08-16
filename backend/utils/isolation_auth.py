import os
import jwt
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone
from fastapi import Request, HTTPException
from fastapi.security import HTTPBearer
from backend.utils.db_utils import get_db
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

logger = logging.getLogger("Incident Ops Logger")
security = HTTPBearer()

_cached_jwks = None
JWT_CLOCK_SKEW_SECONDS = 60
GUEST_ACCESS_ENABLED = os.getenv("ENABLE_GUEST_ACCESS", "false").lower() == "true"

class MockUser:
    def __init__(self, email: str):
        self.sub = email
        self.email = email

def get_clerk_public_key():
    """Fetches and caches Clerk's JWKS public keys."""
    global _cached_jwks
    if _cached_jwks is None:
        clerk_issuer = os.environ.get("CLERK_ISSUER")
        if not clerk_issuer:
            logger.error("CLERK_ISSUER environment variable is not set!")
            raise HTTPException(status_code=500, detail="Auth configuration error: CLERK_ISSUER missing")
        
        jwks_url = f"{clerk_issuer.rstrip('/')}/.well-known/jwks.json"
        try:
            res = requests.get(jwks_url, timeout=5)
            res.raise_for_status()
            _cached_jwks = res.json()
        except Exception as e:
            logger.error(f"Failed to fetch Clerk JWKS: {e}")
            raise HTTPException(status_code=500, detail="Could not reach identity provider")

    return _cached_jwks


def decode_access_token(token: str) -> dict:
    if token == "guest-sandbox-token" and GUEST_ACCESS_ENABLED:
        logger.info("Guest session detected. Bypassing JWT verification.")
        return {
            "sub": "user_guest_sandbox_123",
            "email": "guest@example.com",
            "username": "guest-recruiter@example.com",
            "full_name": "Guest Recruiter",
        }

    try:
        header = jwt.get_unverified_header(token)
        jwks = get_clerk_public_key()
        key_data = next((key for key in jwks["keys"] if key["kid"] == header["kid"]), None)
        if not key_data:
            raise HTTPException(status_code=401, detail="Invalid token key ID")

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            leeway=JWT_CLOCK_SKEW_SECONDS,
        )
        if "username" not in payload:
            payload["username"] = payload.get("sub")
        return payload
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Manual JWT verification failed: %s", exc)
        raise HTTPException(status_code=401, detail="Authentication failed") from exc

async def get_current_user(request: Request) -> dict:
    """Extracts and validates the Bearer JWT or handles the guest sandbox bypass,

    then auto-provisions or retrieves the user's RBAC profile from MongoDB 'directory'.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = auth_header.split(" ")[1]
    
    payload = decode_access_token(token)

    # DIRECTORY AUTO-PROVISIONING & RBAC SYNC
    db = get_db()
    if db is not None:
        clerk_id = payload.get("sub")
        email = payload.get("email", f"{clerk_id}@example.com")

        # Search for existing user profile in directory
        user_doc = db["directory"].find_one({
            "$or": [
                {"clerk_id": clerk_id},
                {"email": email}
            ]
        })

        # Auto-provision if user doesn't exist yet
        if not user_doc:
            logger.info(f"Auto-provisioning new user into directory for clerk_id={clerk_id}")
            user_doc = {
                "clerk_id": clerk_id,
                "email": email,
                "full_name": payload.get("full_name") or payload.get("name") or "errAgent Operator",
                "groups": ["Developers"], 
                "created_at": datetime.now(timezone.utc)
            }
            res = db["directory"].insert_one(user_doc)
            user_doc["_id"] = str(res.inserted_id)
        else:
            user_doc["_id"] = str(user_doc["_id"])

        # Attach directory DB fields (groups, db id) directly onto returned user payload
        payload["groups"] = user_doc.get("groups", [])
        payload["directory_id"] = user_doc.get("_id")
        payload["full_name"] = user_doc.get("full_name")

    return payload

def record_login_event(user_id: str, email: str, is_guest: bool = False, ip_address: str = None):
    """Writes a single login document to MongoDB."""
    try:
        db = get_db()
        if db is None:
            return

        db["login_logs"].insert_one({
            "user_id": user_id,
            "email": email,
            "is_guest": is_guest,
            "ip_address": ip_address,
            "logged_at": datetime.now(timezone.utc)
        })
        logger.info(f"Recorded login for: {email} (Guest={is_guest})")
    except Exception as e:
        logger.error(f"Failed to record login in MongoDB: {e}")