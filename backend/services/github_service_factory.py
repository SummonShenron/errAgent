# backend/services/github_service_factory.py
"""Per-team GitHubOpsService construction (multi-tenancy Phase D).

Replaces the pre-Phase-D pattern of one module-level GitHubOpsService() singleton built from
a single shared GITHUB_TOKEN env var. Call sites that actually write to a team's repository
(commit + PR + merge) must resolve their client through here so a team's own configured
credential — not another team's, and not a stale global fallback — is what touches their repo.
"""
from typing import Any

from fastapi import HTTPException

from backend.services.github_service import GitHubOpsService
from backend.utils.crypto_utils import decrypt_secret


def get_github_service_for_team(db, team_id: Any) -> GitHubOpsService:
    """Raises the same 503 GitHubOpsService._require_token() would raise for a missing token,
    so callers don't need a separate branch for "team exists but has no credential yet" vs.
    "credential configured but empty" — both surface as "not configured"."""
    team = db["teams"].find_one({"_id": team_id}) if team_id is not None else None
    if not team or not team.get("github_pat_encrypted"):
        raise HTTPException(
            status_code=503,
            detail="This team has not configured a GitHub credential yet. "
            "A team manager can set one from the team settings panel.",
        )
    pat = decrypt_secret(team["github_pat_encrypted"])
    return GitHubOpsService(token=pat)
