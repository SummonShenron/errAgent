# backend/utils/team_utils.py
"""Team membership helpers.

Small, dedicated functions in the same style as local-rag's `verify_paapp_access`/
`get_accessible_affiliates` (`local-rag/backend/utils/isolation_kb_utils.py`) — not a generic
RBAC engine. Team *membership* deliberately reuses the existing `directory.groups` mechanism
(`backend/middleware/rbac.py`'s `require_role`) via a namespaced `"team:<slug>"` entry, rather
than introducing a second identity/authorization store. Team *metadata* (name, GitHub
credential, registered services) lives in a first-class `teams` collection instead, since that
data has nowhere to live in a bare group string.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger("Incident Ops Logger")

TEAM_GROUP_PREFIX = "team:"
TEAM_MANAGER_SUFFIX = ":manager"
RESERVED_GROUP_NAMES = {"developers", "incident_managers", "global_admins"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")
BOOTSTRAP_TEAM_SLUG = os.getenv("ERRAGENT_BOOTSTRAP_TEAM_SLUG", "core")


def team_group(slug: str) -> str:
    return f"{TEAM_GROUP_PREFIX}{slug}"


def team_manager_group(slug: str) -> str:
    """A manager is always also stamped with the plain team_group() tag (see add_team_member/
    set_team_member_role) — this tag only ever adds privilege on top of membership, it never
    stands alone."""
    return f"{TEAM_GROUP_PREFIX}{slug}{TEAM_MANAGER_SUFFIX}"


def is_global_admin(current_user: dict) -> bool:
    return "Global_Admins" in (current_user.get("groups") or [])


def get_user_team_slugs(current_user: dict) -> list[str]:
    """Every team this user belongs to, derived from the groups already attached to
    current_user by get_current_user — no extra DB round-trip needed. Excludes manager tags
    (team:<slug>:manager) — those aren't slugs, see get_managed_team_slugs for those."""
    groups = current_user.get("groups") or []
    return [
        g[len(TEAM_GROUP_PREFIX):]
        for g in groups
        if g.startswith(TEAM_GROUP_PREFIX) and not g.endswith(TEAM_MANAGER_SUFFIX)
    ]


def get_managed_team_slugs(current_user: dict) -> list[str]:
    """Every team this user manages (can edit membership/GitHub credential for)."""
    groups = current_user.get("groups") or []
    return [
        g[len(TEAM_GROUP_PREFIX):-len(TEAM_MANAGER_SUFFIX)]
        for g in groups
        if g.startswith(TEAM_GROUP_PREFIX) and g.endswith(TEAM_MANAGER_SUFFIX)
    ]


def is_team_manager(current_user: dict, slug: str) -> bool:
    """Global_Admins bypasses; everyone else must hold team:<slug>:manager."""
    if is_global_admin(current_user):
        return True
    return slug in get_managed_team_slugs(current_user)


def validate_team_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="Team slug must be 3-50 characters: lowercase letters, digits, and hyphens only.",
        )
    if slug in RESERVED_GROUP_NAMES:
        raise HTTPException(status_code=400, detail=f"Team slug '{slug}' collides with a reserved role name.")
    return slug


def _serialize_team(team: dict) -> dict:
    team = dict(team)
    team["_id"] = str(team["_id"])
    team["github_configured"] = bool(team.pop("github_pat_encrypted", None))
    configured_at = team.get("github_pat_configured_at")
    team["github_pat_configured_at"] = configured_at.isoformat() if isinstance(configured_at, datetime) else configured_at
    return team


def create_team(
    db, *, name: str, slug: str, created_by: str, creator_clerk_id: str | None = None
) -> dict:
    slug = validate_team_slug(slug)
    if db["teams"].find_one({"slug": slug}):
        raise HTTPException(status_code=409, detail=f"Team slug '{slug}' already exists.")

    now = datetime.now(timezone.utc)
    document = {
        "name": (name or slug).strip(),
        "slug": slug,
        "created_by": created_by,
        "created_at": now,
        "github_pat_encrypted": None,
        "github_pat_configured_at": None,
    }
    result = db["teams"].insert_one(document)
    document["_id"] = result.inserted_id
    logger.info("Created team '%s' (slug=%s) by %s", document["name"], slug, created_by)

    if creator_clerk_id:
        # The creator becomes the team's first manager so they can immediately configure a
        # GitHub credential and add members without a second Global_Admins round-trip.
        try:
            add_team_member(
                db, slug=slug, target_clerk_id=creator_clerk_id, target_email=None,
                added_by=created_by, role="manager",
            )
        except HTTPException:
            logger.warning("Could not auto-grant manager role to team creator (clerk_id=%s)", creator_clerk_id)

    return _serialize_team(document)


def _find_directory_user(db, *, target_clerk_id: str | None, target_email: str | None) -> dict:
    or_clauses: list[dict[str, Any]] = []
    if target_clerk_id:
        or_clauses.append({"clerk_id": target_clerk_id})
    if target_email:
        or_clauses.append({"email": target_email})
    if not or_clauses:
        raise HTTPException(status_code=400, detail="Provide clerk_id or email.")

    user_doc = db["directory"].find_one({"$or": or_clauses})
    if not user_doc:
        raise HTTPException(status_code=404, detail="No directory user found matching that clerk_id/email.")
    return user_doc


def add_team_member(
    db,
    *,
    slug: str,
    target_clerk_id: str | None,
    target_email: str | None,
    added_by: str,
    role: str = "member",
) -> dict:
    if role not in {"member", "manager"}:
        raise HTTPException(status_code=400, detail="role must be 'member' or 'manager'.")
    team = db["teams"].find_one({"slug": slug})
    if not team:
        raise HTTPException(status_code=404, detail=f"Team not found: {slug}")

    user_doc = _find_directory_user(db, target_clerk_id=target_clerk_id, target_email=target_email)

    groups_to_add = [team_group(slug)]
    if role == "manager":
        groups_to_add.append(team_manager_group(slug))
    db["directory"].update_one({"_id": user_doc["_id"]}, {"$addToSet": {"groups": {"$each": groups_to_add}}})
    member_label = user_doc.get("email") or user_doc.get("clerk_id")
    logger.info("Added %s to team %s as %s (by %s)", member_label, slug, role, added_by)
    return {"team": slug, "member": member_label, "role": role}


def set_team_member_role(
    db, *, slug: str, target_clerk_id: str | None, target_email: str | None, role: str, updated_by: str
) -> dict:
    """Promote/demote an existing team member. The target must already be a member — this
    only ever changes the manager tag, never grants membership itself (use add_team_member
    for that)."""
    if role not in {"member", "manager"}:
        raise HTTPException(status_code=400, detail="role must be 'member' or 'manager'.")
    team = db["teams"].find_one({"slug": slug})
    if not team:
        raise HTTPException(status_code=404, detail=f"Team not found: {slug}")

    user_doc = _find_directory_user(db, target_clerk_id=target_clerk_id, target_email=target_email)
    if team_group(slug) not in (user_doc.get("groups") or []):
        raise HTTPException(status_code=400, detail="User is not a member of this team yet.")

    manager_tag = team_manager_group(slug)
    if role == "manager":
        db["directory"].update_one({"_id": user_doc["_id"]}, {"$addToSet": {"groups": manager_tag}})
    else:
        db["directory"].update_one({"_id": user_doc["_id"]}, {"$pull": {"groups": manager_tag}})
    member_label = user_doc.get("email") or user_doc.get("clerk_id")
    logger.info("Set %s role on team %s to %s (by %s)", member_label, slug, role, updated_by)
    return {"team": slug, "member": member_label, "role": role}


def remove_team_member(
    db, *, slug: str, target_clerk_id: str | None, target_email: str | None, removed_by: str
) -> dict:
    team = db["teams"].find_one({"slug": slug})
    if not team:
        raise HTTPException(status_code=404, detail=f"Team not found: {slug}")

    user_doc = _find_directory_user(db, target_clerk_id=target_clerk_id, target_email=target_email)
    db["directory"].update_one(
        {"_id": user_doc["_id"]},
        {"$pull": {"groups": {"$in": [team_group(slug), team_manager_group(slug)]}}},
    )
    member_label = user_doc.get("email") or user_doc.get("clerk_id")
    logger.info("Removed %s from team %s (by %s)", member_label, slug, removed_by)
    return {"team": slug, "member": member_label}


def set_team_github_pat(db, *, slug: str, pat: str, set_by: str) -> dict:
    from backend.utils.crypto_utils import encrypt_secret

    pat = (pat or "").strip()
    if not pat:
        raise HTTPException(status_code=400, detail="pat is required.")
    team = db["teams"].find_one({"slug": slug})
    if not team:
        raise HTTPException(status_code=404, detail=f"Team not found: {slug}")

    now = datetime.now(timezone.utc)
    db["teams"].update_one(
        {"_id": team["_id"]},
        {"$set": {
            "github_pat_encrypted": encrypt_secret(pat),
            "github_pat_configured_at": now,
            "github_pat_configured_by": set_by,
        }},
    )
    logger.info("GitHub credential set for team %s (by %s)", slug, set_by)
    return {"team": slug, "github_configured": True, "github_pat_configured_at": now}


def clear_team_github_pat(db, *, slug: str, cleared_by: str) -> dict:
    team = db["teams"].find_one({"slug": slug})
    if not team:
        raise HTTPException(status_code=404, detail=f"Team not found: {slug}")

    db["teams"].update_one(
        {"_id": team["_id"]},
        {"$set": {"github_pat_encrypted": None, "github_pat_configured_at": None, "github_pat_configured_by": None}},
    )
    logger.info("GitHub credential cleared for team %s (by %s)", slug, cleared_by)
    return {"team": slug, "github_configured": False}


def list_team_members(db, slug: str) -> list[dict]:
    member_group = team_group(slug)
    manager_group = team_manager_group(slug)
    members = []
    for user_doc in db["directory"].find({"groups": member_group}):
        members.append({
            "clerk_id": user_doc.get("clerk_id"),
            "email": user_doc.get("email"),
            "full_name": user_doc.get("full_name"),
            "role": "manager" if manager_group in (user_doc.get("groups") or []) else "member",
        })
    return members


def list_teams_for_user(db, current_user: dict) -> list[dict]:
    """Global_Admins sees every team; everyone else sees only teams they belong to."""
    if is_global_admin(current_user):
        teams = db["teams"].find({})
    else:
        slugs = get_user_team_slugs(current_user)
        teams = db["teams"].find({"slug": {"$in": slugs}}) if slugs else []
    return [_serialize_team(team) for team in teams]


def get_user_team_ids(db, current_user: dict) -> list[Any]:
    """Resolve the caller's `team:<slug>` group entries to actual `teams._id` values, for
    `$in`-filtering documents that carry `team_id` (not a slug) — incidents, proposals, etc."""
    slugs = get_user_team_slugs(current_user)
    if not slugs:
        return []
    return [team["_id"] for team in db["teams"].find({"slug": {"$in": slugs}})]


def user_can_access_team_id(db, current_user: dict, team_id: Any) -> bool:
    """Global_Admins bypasses; everyone else must belong to the team that owns team_id."""
    if is_global_admin(current_user):
        return True
    if team_id is None:
        return False
    return team_id in get_user_team_ids(db, current_user)


_BOOTSTRAP_TEAM_ID_CACHE: dict[str, Any] = {"team_id": None, "loaded_at": 0.0, "db_id": None}
_BOOTSTRAP_TEAM_ID_CACHE_TTL_SECONDS = 30.0


def get_bootstrap_team_id(db) -> Any:
    """The fallback team for incidents/proposals with no other natural owner (legacy
    shared-secret ingest clients, errAgent's own self-monitor, unauthenticated log-drain
    webhooks). Cached briefly with the same db-identity invalidation as the service registry
    cache (app_utils.py) so tests using a fresh FakeDB per test stay isolated."""
    now = time.monotonic()
    db_id = id(db)
    stale = (
        db_id != _BOOTSTRAP_TEAM_ID_CACHE["db_id"]
        or now - _BOOTSTRAP_TEAM_ID_CACHE["loaded_at"] > _BOOTSTRAP_TEAM_ID_CACHE_TTL_SECONDS
    )
    if stale:
        team = db["teams"].find_one({"slug": BOOTSTRAP_TEAM_SLUG})
        _BOOTSTRAP_TEAM_ID_CACHE["team_id"] = team["_id"] if team else None
        _BOOTSTRAP_TEAM_ID_CACHE["loaded_at"] = now
        _BOOTSTRAP_TEAM_ID_CACHE["db_id"] = db_id
    return _BOOTSTRAP_TEAM_ID_CACHE["team_id"]
