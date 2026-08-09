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

async def get_current_user(request: Request) -> dict:
    """Extracts and validates the Bearer JWT or handles the guest sandbox bypass."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    token = auth_header.split(" ")[1]
    
    # 1. GUEST BYPASS: Check for sandbox token first
    if token == "guest-sandbox-token":
        logger.info("Guest session detected. Bypassing JWT verification.")
        return {
            "sub": "guest-recruiter@example.com",
            "email": "guest@example.com",
            "username": "guest-recruiter@example.com"
        }

    # 2. Clerk JWT verification logic
    try:
        # Get header to find the 'kid' (Key ID)
        header = jwt.get_unverified_header(token)
        jwks = get_clerk_public_key()
        
        # Find matching key
        key_data = next((k for k in jwks['keys'] if k['kid'] == header['kid']), None)
        if not key_data:
            raise HTTPException(status_code=401, detail="Invalid token key ID")

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        
        # Verify and decode
        payload = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            leeway=JWT_CLOCK_SKEW_SECONDS,
        )
        
        # Ensure 'username' exists for compatibility with rbac.py
        if "username" not in payload:
            payload["username"] = payload.get("sub")

        return payload 
        
    except Exception as e:
        logger.error(f"Manual JWT verification failed: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed")

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