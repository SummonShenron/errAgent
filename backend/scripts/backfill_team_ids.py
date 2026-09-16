"""One-time backfill: stamp team_id on pre-Phase-C documents that predate multi-tenancy
(incidents, patchy_proposals, remediations). Idempotent — safe to re-run; only touches
documents missing team_id. remediations always inherit their parent incident's team_id
rather than resolving independently, to avoid drift between the two.

Usage:
    python -m backend.scripts.backfill_team_ids [--dry-run] [--team-slug core]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--team-slug", default=os.getenv("ERRAGENT_BOOTSTRAP_TEAM_SLUG", "core"))
    args = parser.parse_args()

    client = MongoClient(os.environ["MONGO_URI"])
    db = client[os.environ.get("MONGO_DB_NAME", "errAgent_DB")]

    team = db["teams"].find_one({"slug": args.team_slug})
    if not team:
        print(f"ERROR: bootstrap team '{args.team_slug}' does not exist. Create it before backfilling.")
        sys.exit(1)
    team_id = team["_id"]
    print(f"Backfilling missing team_id -> {args.team_slug} ({team_id})")

    incidents_missing = db["incidents"].count_documents({"team_id": {"$exists": False}})
    print(f"incidents missing team_id: {incidents_missing}")
    if not args.dry_run and incidents_missing:
        result = db["incidents"].update_many({"team_id": {"$exists": False}}, {"$set": {"team_id": team_id}})
        print(f"  -> updated {result.modified_count}")

    proposals_missing = db["patchy_proposals"].count_documents({"team_id": {"$exists": False}})
    print(f"patchy_proposals missing team_id: {proposals_missing}")
    if not args.dry_run and proposals_missing:
        result = db["patchy_proposals"].update_many({"team_id": {"$exists": False}}, {"$set": {"team_id": team_id}})
        print(f"  -> updated {result.modified_count}")

    # remediations inherit their parent incident's team_id, not the bootstrap team directly,
    # so a remediation whose incident belongs to a different team stays correctly attributed.
    remediations_missing = list(db["remediations"].find({"team_id": {"$exists": False}}, {"_id": 1, "incident_id": 1}))
    print(f"remediations missing team_id: {len(remediations_missing)}")
    updated_remediations = 0
    for remediation in remediations_missing:
        incident = db["incidents"].find_one({"_id": remediation.get("incident_id")}, {"team_id": 1})
        resolved_team_id = incident.get("team_id") if incident else team_id
        if not args.dry_run:
            db["remediations"].update_one({"_id": remediation["_id"]}, {"$set": {"team_id": resolved_team_id}})
        updated_remediations += 1
    if not args.dry_run:
        print(f"  -> updated {updated_remediations}")

    if args.dry_run:
        print("\nDry run only — no writes performed. Re-run without --dry-run to apply.")
    else:
        print("\nBackfill complete.")


if __name__ == "__main__":
    main()
