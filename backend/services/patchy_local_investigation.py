"""Local tool-bridge for CLI-typed investigations.

When a developer types a plain-English description into the local erragent daemon's console
(no stack trace, no error) instead of an error being caught, the cloud-side investigation loop
(``backend/services/patchy_investigation.py``) needs to read the developer's actual local files
— including uncommitted edits — to locate the relevant code. The cloud has no filesystem access;
only the local daemon does, and the daemon has no ability to reason about *what* to read next.

This module bridges the two the same way the daemon already polls the cloud for a proposal
(``sdk/src/erragent/local/daemon.py``'s ``_poll_for_proposal``), just in the other direction:
the cloud-side ``act()`` (see ``build_local_bridge_investigation_tools``) writes a pending
request here and blocks in its own poll loop; the daemon's poll loop notices the pending
request, executes it against the local filesystem, and reports the result back via
``submit_tool_result``, which unblocks the cloud side. No new transport, no new threading model
on either side — just a second thing both sides already know how to do (poll a status).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.services.patchy_investigation import InvestigationTools


def create_tool_request(db, incident_id: str, tool_action: str, args: dict) -> str:
    request_id = uuid4().hex
    now = datetime.now(timezone.utc)
    db["local_investigation_requests"].insert_one({
        "_id": request_id,
        "incident_id": incident_id,
        "tool_action": tool_action,
        "args": args,
        "status": "pending",
        "observation": None,
        "created_at": now,
        "answered_at": None,
    })
    return request_id


def get_pending_tool_request(db, incident_id: str) -> dict[str, Any] | None:
    """Returns the oldest still-pending request for this incident, or None. Polled by the
    local daemon — see the loop this feeds in ``sdk/src/erragent/local/daemon.py``."""
    return db["local_investigation_requests"].find_one(
        {"incident_id": incident_id, "status": "pending"},
        sort=[("created_at", 1)],
    )


def submit_tool_result(db, incident_id: str, request_id: str, observation: str) -> None:
    db["local_investigation_requests"].update_one(
        {"_id": request_id, "incident_id": incident_id},
        {"$set": {
            "status": "answered",
            "observation": observation,
            "answered_at": datetime.now(timezone.utc),
        }},
    )


def build_local_bridge_investigation_tools(
    db, incident_id: str, poll_interval: float = 1.0, poll_timeout: float = 30.0
) -> InvestigationTools:
    """Read-only, local-filesystem-backed tools for the investigation loop, answered live by
    the developer's own erragent daemon rather than fetched from GitHub. Only two actions for
    now — read a file, list the tree — matching how the GitHub-backed tools were scoped down to
    read-only essentials in the previous phase; local git diff/log is a natural v2 addition.
    """

    def act(tool_action: str, args: dict) -> str:
        if tool_action not in {"read_local_file", "list_local_tree"}:
            return f"ERROR: unrecognized tool_action '{tool_action}'"

        request_id = create_tool_request(db, incident_id, tool_action, args)
        deadline = time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            request = db["local_investigation_requests"].find_one({"_id": request_id})
            if request and request.get("status") == "answered":
                return request.get("observation") or ""
            time.sleep(poll_interval)
        return "ERROR: local daemon did not respond to the tool request in time"

    actions_menu = "\n".join([
        "- read_local_file — args: path (relative file path within the project root)",
        "- list_local_tree — no args; lists every file path in the local project root",
    ])

    return InvestigationTools(actions_menu=actions_menu, act=act)
