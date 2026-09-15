"""Proposal lifecycle for local-dev-mode remediations.

Unlike every other Patchy proposal kind, approving a ``local_patch`` proposal does not execute
anything on the errAgent backend — the backend has no filesystem access to the developer's
machine. Approval here only claims the proposal and hands back sandbox-verified content; the
*local erragent daemon* (the only party that can actually write the file) performs the write and
reports the outcome back via ``ack_local_patch``. This mirrors the claim-then-execute pattern used
throughout ``patchy_hitl.py``, split across two calls because the "execute" step happens off-box.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.services.patchy_hitl import PatchyProposalError, get_proposal
from backend.utils.app_utils import serialize_mongo_doc

logger = logging.getLogger("errAgent Logger")


def create_local_patch_proposal(db, incident_id: str, remediation: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    proposal_id = f"patchy_{uuid4().hex}"
    document = {
        "_id": proposal_id,
        "kind": "local_patch",
        "risk": "local_write",
        "status": "awaiting_approval",
        "summary": f"Apply local fix to {remediation.get('target_file_path', 'unknown file')}",
        "action": {
            "incident_id": incident_id,
            "target_file_path": remediation.get("target_file_path"),
            "diff_preview": remediation.get("code_patch"),
            "pr_title": remediation.get("pr_title"),
        },
        "created_by": "SYSTEM_AI",
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].insert_one(document)
    logger.info("--> [errAgent local_patch] Created proposal %s for incident %s", proposal_id, incident_id)
    return serialize_mongo_doc(document)


def get_local_patch_status(db, incident_id: str) -> dict[str, Any]:
    """Polled by the local daemon after reporting an incident, to learn when analysis has
    finished and a proposal is ready for local approval."""
    incident = db["incidents"].find_one({"_id": incident_id})
    if not incident:
        raise PatchyProposalError(f"Incident not found: {incident_id}")

    remediation = db["remediations"].find_one(
        {"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]
    )
    proposal = db["patchy_proposals"].find_one(
        {"kind": "local_patch", "action.incident_id": incident_id},
        sort=[("created_at", -1)],
    )

    return {
        "incident_id": incident_id,
        "incident_status": incident.get("status"),
        "remediation_status": remediation.get("status") if remediation else None,
        "failure_reason": remediation.get("failure_reason") if remediation else None,
        "proposal": serialize_mongo_doc(proposal) if proposal else None,
    }


def approve_local_patch(db, proposal_id: str, actor: str) -> dict[str, Any]:
    """Claim the proposal and return sandbox-verified content for the daemon to write.

    Does NOT touch any filesystem — the caller (the local daemon) is responsible for verifying
    the content again before writing (defense in depth) and for calling ``ack_local_patch``
    afterward with the outcome.
    """
    proposal = get_proposal(db, proposal_id)
    if proposal.get("kind") != "local_patch":
        raise PatchyProposalError(f"Not a local_patch proposal: {proposal_id}")

    now = datetime.now(timezone.utc)
    claimed = db["patchy_proposals"].update_one(
        {"_id": proposal_id, "status": "awaiting_approval"},
        {"$set": {"status": "running", "approved_by": actor, "approved_at": now, "updated_at": now}},
    )
    if claimed.modified_count != 1:
        raise PatchyProposalError(f"Proposal cannot be approved from status: {proposal.get('status', 'unknown')}")

    incident_id = proposal["action"]["incident_id"]
    remediation = db["remediations"].find_one(
        {"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]
    )
    if not remediation or remediation.get("content_source") != "sandbox_applied":
        # Revert the claim so the proposal doesn't get stuck "running" with nothing to hand out.
        db["patchy_proposals"].update_one(
            {"_id": proposal_id},
            {"$set": {"status": "failed", "result": {"reason": "remediation content unavailable or unverified"}, "updated_at": now}},
        )
        raise PatchyProposalError(f"No sandbox-verified remediation found for incident: {incident_id}")

    return {
        "proposal_id": proposal_id,
        "incident_id": incident_id,
        "target_file_path": remediation.get("target_file_path"),
        "full_file_content": remediation.get("full_file_content"),
        "full_file_content_sha256": remediation.get("full_file_content_sha256"),
        "base_file_sha256": remediation.get("base_file_sha256"),
        "code_patch": remediation.get("code_patch"),
        "content_source": remediation.get("content_source"),
    }


def ack_local_patch(db, proposal_id: str, actor: str, outcome: str, detail: str | None = None) -> dict[str, Any]:
    """Called by the local daemon after attempting the write. The backend can't observe this on
    its own — the write happens on the developer's machine — so the daemon reports back."""
    if outcome not in {"succeeded", "failed"}:
        raise PatchyProposalError("outcome must be 'succeeded' or 'failed'")

    proposal = get_proposal(db, proposal_id)
    if proposal.get("kind") != "local_patch":
        raise PatchyProposalError(f"Not a local_patch proposal: {proposal_id}")
    if proposal.get("status") != "running":
        raise PatchyProposalError(f"Proposal is not awaiting an ack (status={proposal.get('status')})")

    now = datetime.now(timezone.utc)
    db["patchy_proposals"].update_one(
        {"_id": proposal_id},
        {"$set": {
            "status": outcome,
            "result": {"detail": detail},
            "completed_at": now,
            "updated_at": now,
        }},
    )

    incident_id = proposal["action"]["incident_id"]
    db["remediations"].update_one(
        {"incident_id": incident_id},
        {"$set": {
            "status": "executed" if outcome == "succeeded" else "local_write_failed",
            "local_write_actor": actor,
            "local_write_detail": detail,
            "updated_at": now,
        }},
    )
    db["incidents"].update_one(
        {"_id": incident_id},
        {"$set": {"status": "resolved" if outcome == "succeeded" else "fix_proposed", "updated_at": now}},
    )
    db["audit_logs"].insert_one({
        "incident_id": incident_id,
        "actor": actor,
        "action": "LOCAL_PATCH_APPROVED_AND_WRITTEN" if outcome == "succeeded" else "LOCAL_PATCH_WRITE_FAILED",
        "details": {"proposal_id": proposal_id, "detail": detail},
        "timestamp": now,
    })

    return serialize_mongo_doc(db["patchy_proposals"].find_one({"_id": proposal_id}))
