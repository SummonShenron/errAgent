"""Create, rotate or disable an app's READ credential (see backend/utils/app_read_utils.py).

    python -m backend.scripts.manage_app_read_access services --team-slug core
    python -m backend.scripts.manage_app_read_access enable  --app-id saapp --service saapp [--team-slug core] \\
                                                            [--render-service-id srv-xxxx] [--create]
    python -m backend.scripts.manage_app_read_access rotate  --app-id saapp
    python -m backend.scripts.manage_app_read_access disable --app-id saapp

`services` lists the service names that incidents are actually stored under, because a read credential is scoped to those exact
names (an app can only ever read incidents filed under a service name written on its own record). The read secret is shown ONCE
and only its SHA-256 hash is stored, so a lost secret is rotated, never recovered. `--create` is for an app that has no
`ingest_clients` record yet (an app still on the legacy shared ingest secret): it also creates a per-app INGEST credential, shown
once, which the app should switch to so its incidents carry its app id.
"""
import argparse
import os
import secrets
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from backend.utils.app_read_utils import hash_read_secret, new_read_secret  # noqa: E402


def resolve_team_id(db, team_slug: Optional[str]):
    if team_slug:
        team = db["teams"].find_one({"slug": team_slug})
        if not team:
            raise ValueError(f"No team with slug {team_slug!r}.")
        return team["_id"]
    from backend.utils.team_utils import get_bootstrap_team_id

    return get_bootstrap_team_id(db)


def suggest_service_names(db, team_id) -> List[str]:
    """The service names incidents are stored under for a team, so the right ones go on the credential."""
    names = {str(i.get("service_name")).strip().lower() for i in db["incidents"].find({"team_id": team_id}, {"service_name": 1})
             if i.get("service_name")}
    return sorted(names)


def enable_read_access(
    db,
    *,
    app_id: str,
    service_names: List[str],
    team_id: Any,
    render_service_id: Optional[str] = None,
    create: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Optional[str]]:
    """Gives `app_id` a read credential scoped to `service_names` within `team_id`. Returns the secrets, shown once."""
    names = sorted({str(s).strip().lower() for s in service_names if str(s).strip()})
    if not names:
        raise ValueError("At least one service name is required: a read credential is scoped to named services.")
    now = now or datetime.now(timezone.utc)
    client = db["ingest_clients"].find_one({"app_id": app_id})
    ingest_secret: Optional[str] = None
    if client is None:
        if not create:
            raise ValueError(f"No ingest client {app_id!r}. Pass --create to create one (it also gets an ingest secret).")
        ingest_secret = secrets.token_urlsafe(32)
        db["ingest_clients"].insert_one({
            "app_id": app_id, "secret": ingest_secret, "enabled": True, "team_id": team_id, "created_at": now,
        })
    elif client.get("team_id") not in (None, team_id):
        raise ValueError(f"{app_id!r} belongs to a different team; refusing to scope reads across teams.")

    read_secret = new_read_secret()
    update: Dict[str, Any] = {
        "read_secret_sha256": hash_read_secret(read_secret),
        "read_service_names": names,
        "team_id": team_id,
        "read_enabled_at": now,
    }
    if render_service_id:
        update["render_service_id"] = render_service_id.strip()
    db["ingest_clients"].update_one({"app_id": app_id}, {"$set": update})
    return {"app_id": app_id, "read_secret": read_secret, "ingest_secret": ingest_secret}


def rotate_read_secret(db, *, app_id: str, now: Optional[datetime] = None) -> str:
    client = db["ingest_clients"].find_one({"app_id": app_id})
    if not client or not client.get("read_secret_sha256"):
        raise ValueError(f"{app_id!r} has no read credential to rotate. Use `enable`.")
    secret = new_read_secret()
    db["ingest_clients"].update_one(
        {"app_id": app_id},
        {"$set": {"read_secret_sha256": hash_read_secret(secret), "read_rotated_at": now or datetime.now(timezone.utc)}},
    )
    return secret


def disable_read_access(db, *, app_id: str) -> bool:
    """Removes the read credential (the app's ingest credential is untouched). True if there was one to remove."""
    client = db["ingest_clients"].find_one({"app_id": app_id})
    if not client or not client.get("read_secret_sha256"):
        return False
    db["ingest_clients"].update_one({"app_id": app_id}, {"$unset": {"read_secret_sha256": "", "read_service_names": ""}})
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("services", "enable", "rotate", "disable"):
        p = sub.add_parser(name)
        if name in ("services", "enable"):
            p.add_argument("--team-slug", default=None, help="Team slug (default: the bootstrap team).")
        if name != "services":
            p.add_argument("--app-id", required=True)
        if name == "enable":
            p.add_argument("--service", action="append", required=True, help="A service name incidents are stored under (repeatable).")
            p.add_argument("--render-service-id", default=None)
            p.add_argument("--create", action="store_true", help="Create the ingest client if it does not exist.")
    args = parser.parse_args(argv)

    from backend.utils.db_utils import get_db

    db = get_db()
    if db is None:
        print("MongoDB is not configured (USE_DB). Aborting.", file=sys.stderr)
        return 1
    try:
        if args.command == "services":
            team_id = resolve_team_id(db, args.team_slug)
            print("Service names with incidents for this team:")
            for name in suggest_service_names(db, team_id) or ["(none yet)"]:
                print(f"  {name}")
        elif args.command == "enable":
            result = enable_read_access(
                db, app_id=args.app_id, service_names=args.service, team_id=resolve_team_id(db, args.team_slug),
                render_service_id=args.render_service_id, create=args.create,
            )
            print(f"Read access enabled for {result['app_id']}.")
            print("Shown once; store them now:")
            print(f"  ERRAGENT_APP_ID={result['app_id']}")
            print(f"  ERRAGENT_READ_SECRET={result['read_secret']}")
            if result["ingest_secret"]:
                print(f"  ERRAGENT_APP_SECRET={result['ingest_secret']}   (new per-app ingest secret)")
        elif args.command == "rotate":
            print(f"New read secret for {args.app_id} (shown once): {rotate_read_secret(db, app_id=args.app_id)}")
        elif args.command == "disable":
            print("Read access removed." if disable_read_access(db, app_id=args.app_id) else "Nothing to remove.")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
