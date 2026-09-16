# backend/utils/crypto_utils.py
"""Symmetric encryption for secrets stored at rest (currently just per-team GitHub PATs,
teams.github_pat_encrypted — see team_utils.set_team_github_pat). Uses Fernet (AES-128-CBC +
HMAC, from the `cryptography` package already in requirements.txt) rather than anything
bespoke. The key must be a Fernet-format key (44-char urlsafe-base64); generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

_ENV_VAR = "TEAM_SECRET_ENCRYPTION_KEY"


def _get_fernet() -> Fernet:
    key = os.getenv(_ENV_VAR)
    if not key:
        raise HTTPException(
            status_code=503,
            detail=f"Secret storage is not configured. Set {_ENV_VAR} and restart errAgent.",
        )
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=500, detail=f"{_ENV_VAR} is not a valid Fernet key.") from exc


def encrypt_secret(plaintext: str) -> bytes:
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes | str) -> str:
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode("utf-8")
    try:
        return _get_fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Stored secret could not be decrypted (key rotated?).") from exc
