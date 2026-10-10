"""Read access for the apps that report to errAgent: an app reads ITS OWN incidents and recent log lines, nothing else.

Everything else in errAgent that reads incidents authenticates a human (Clerk) and scopes by that person's teams. An app that
wants to look at its own incidents (SAAPP's admin tool, for example) has no human session, and the credential it already holds
is the INGEST secret: a write credential that sits in every reporting app's environment, in plaintext. Reusing it for reads
would turn any app's reporting key into a key to read data. So reads get their own credential, with these properties:

- SEPARATE. `x-read-secret`, distinct from the ingest secret; stored only as a SHA-256 hash, shown once when created.
- SCOPED BY CONSTRUCTION. The credential resolves to a team and a list of canonical service names stored on the app's
  `ingest_clients` record. Every query is filtered by both, so an app can never name another app's data. A record with no
  team or no service names cannot read at all (403), so a half-configured legacy client fails closed.
- NO ENUMERATION. An unknown app, a disabled app, a missing secret and a wrong secret all give the same 401.
- READ-ONLY AND BOUNDED. A fixed set of fields, text truncated, a page size cap, a look-back cap.
- NO RAW CONTENT. Incident `metadata` and `context` can hold whatever the reporting app attached (SAAPP used to attach whole
  conversation states). Only an allowlist of operational keys is returned, each value a short scalar; everything else is dropped,
  so an app that has not cleaned its logs cannot leak through this path.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from backend.utils.app_utils import FREETEXT_INVESTIGATION_SOURCE_TAG

MAX_LOOKBACK_DAYS = 30
DEFAULT_LOOKBACK_DAYS = 7
MAX_PAGE_SIZE = 50
MAX_MESSAGE_CHARS = 300
MAX_ROOT_CAUSE_CHARS = 500
MAX_STACK_CHARS = 4000
MAX_FIX_CHARS = 1500
MAX_META_VALUE_CHARS = 200

# Operational facts about where and when something failed. Anything not listed here is dropped, never passed through.
METADATA_ALLOWLIST = (
    "source", "log_level", "logger", "module", "function", "line", "environment", "workflowName", "requestId", "node",
    "statusCode", "method", "path", "durationMs", "release", "route", "error_source", "app_id", "local_dev", "target_file_path",
)

_SINCE_RE = re.compile(r"^(\d{1,4})\s*([mhd])$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^[a-z_]{1,24}$")


# ---------------------------------------------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------------------------------------------

def new_read_secret() -> str:
    return "ear_" + secrets.token_urlsafe(32)


def hash_read_secret(secret: str) -> str:
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


_DUMMY_HASH = hash_read_secret("no-such-client")


def authenticate_read_client(db, incoming_secret: Optional[str], app_id: Optional[str]) -> Dict[str, Any]:
    """The read context for this app: `{actor, app_id, team_id, service_names, render_service_id}`. Raises 401 for any bad
    credential (identical for every cause) and 403 for a client that has no team or no service names to scope to."""
    invalid = HTTPException(status_code=401, detail="Invalid read credentials")
    if not incoming_secret or not app_id:
        raise invalid
    client = db["ingest_clients"].find_one({"app_id": app_id, "enabled": True}) or {}
    stored = str(client.get("read_secret_sha256") or "")
    # Always compare, so a missing or disabled client costs the same as a wrong secret.
    matches = hmac.compare_digest(hash_read_secret(incoming_secret), stored or _DUMMY_HASH)
    if not stored or not matches:
        raise invalid
    team_id = client.get("team_id")
    service_names = [str(s).strip().lower() for s in (client.get("read_service_names") or []) if str(s).strip()]
    if team_id is None or not service_names:
        raise HTTPException(status_code=403, detail="This app is not configured for reads.")
    return {
        "actor": f"APP_READ:{app_id}",
        "app_id": app_id,
        "team_id": team_id,
        "service_names": service_names,
        "render_service_id": (client.get("render_service_id") or None),
    }


# ---------------------------------------------------------------------------------------------------------------
# Shaping what is returned
# ---------------------------------------------------------------------------------------------------------------

def _clip(value: Any, limit: int) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[:limit] + "…"


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value) if value else None


def sanitize_metadata(metadata: Any) -> Dict[str, Any]:
    """Only allowlisted keys with short scalar values. Nested data, long text and unknown keys never get through."""
    if not isinstance(metadata, dict):
        return {}
    clean: Dict[str, Any] = {}
    for key in METADATA_ALLOWLIST:
        value = metadata.get(key)
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        clean[key] = value if isinstance(value, (bool, int, float)) else _clip(value, MAX_META_VALUE_CHARS)
    return clean


def sanitize_incident(
    incident: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
    remediation: Optional[Dict[str, Any]] = None,
    *,
    detail: bool = False,
) -> Dict[str, Any]:
    analysis = analysis or {}
    remediation = remediation or {}
    first_line = str(incident.get("error_message") or "").splitlines()[0] if incident.get("error_message") else ""
    out: Dict[str, Any] = {
        "id": str(incident.get("_id")),
        "service": incident.get("service_name"),
        "environment": incident.get("environment"),
        "status": incident.get("status"),
        "message": _clip(first_line, MAX_MESSAGE_CHARS),
        "created_at": _iso(incident.get("created_at")),
        "updated_at": _iso(incident.get("updated_at")),
        "severity": analysis.get("severity"),
        "root_cause": _clip(analysis.get("root_cause_summary"), MAX_ROOT_CAUSE_CHARS) if analysis.get("root_cause_summary") else None,
        "fix_status": remediation.get("status"),
        "pr_url": remediation.get("pr_url") or None,
    }
    if detail:
        out["fingerprint"] = incident.get("fingerprint")
        out["repository"] = incident.get("repository") or None
        out["stack_trace"] = _clip(incident.get("stack_trace"), MAX_STACK_CHARS)
        out["suggested_fix"] = _clip(analysis.get("suggested_fix"), MAX_FIX_CHARS) if analysis.get("suggested_fix") else None
        out["metadata"] = sanitize_metadata(incident.get("metadata"))
    return out


# ---------------------------------------------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------------------------------------------

def parse_since(since: Optional[str], now: datetime) -> datetime:
    """A look-back start: "24h", "7d", "30m", or an ISO timestamp. Defaults to a week; never earlier than 30 days."""
    floor = now - timedelta(days=MAX_LOOKBACK_DAYS)
    if not since:
        return max(now - timedelta(days=DEFAULT_LOOKBACK_DAYS), floor)
    match = _SINCE_RE.match(since.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return max(now - delta, floor)
    try:
        parsed = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="since must look like 30m, 24h, 7d, or an ISO timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(parsed.astimezone(timezone.utc), floor)


def scope_query(context: Dict[str, Any]) -> Dict[str, Any]:
    """The only filter an app's reads ever start from: its team, its service names, and no free-text investigations."""
    return {
        "team_id": context["team_id"],
        "service_name": {"$in": list(context["service_names"])},
        "metadata.source": {"$ne": FREETEXT_INVESTIGATION_SOURCE_TAG},
    }


def _stamp(doc: Dict[str, Any]) -> datetime:
    """When a document was last written, as an aware UTC datetime (MongoDB returns naive ones unless told otherwise)."""
    value = doc.get("updated_at") or doc.get("created_at")
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _latest_by_incident(docs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        key = str(doc.get("incident_id"))
        if key not in latest or _stamp(doc) >= _stamp(latest[key]):
            latest[key] = doc
    return latest


def list_app_incidents(
    db,
    context: Dict[str, Any],
    *,
    status: Optional[str] = None,
    environment: Optional[str] = "production",
    since: Optional[str] = None,
    limit: int = 20,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    if status is not None and not _STATUS_RE.match(status):
        raise HTTPException(status_code=400, detail="Unknown status filter.")
    query = scope_query(context)
    query["created_at"] = {"$gte": parse_since(since, now)}
    if status:
        query["status"] = status
    if environment:
        query["environment"] = environment
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    incidents = list(db["incidents"].find(query).sort("created_at", -1).limit(limit))
    ids = [str(i.get("_id")) for i in incidents]
    analyses = _latest_by_incident(list(db["analyses"].find({"incident_id": {"$in": ids}}))) if ids else {}
    remediations = _latest_by_incident(list(db["remediations"].find({"incident_id": {"$in": ids}}))) if ids else {}
    return [sanitize_incident(i, analyses.get(str(i.get("_id"))), remediations.get(str(i.get("_id")))) for i in incidents]


LOG_LEVELS = {"info": 0, "warn": 1, "error": 2}
MAX_LOG_PAGE = 100
MAX_LOG_MESSAGE_CHARS = 600
DEFAULT_LOG_LOOKBACK = "1h"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-.:]{1,80}$")


def _parse_log_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def list_app_logs(
    entries: List[Dict[str, Any]],
    context: Dict[str, Any],
    *,
    level: Optional[str] = None,
    since: Optional[str] = None,
    contains: Optional[str] = None,
    request_id: Optional[str] = None,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """This app's recent log lines from the live broker (a per-service ring buffer in memory, so it covers "since errAgent last
    restarted, up to the buffer size", not a full history). `entries` are the broker's buffered entries for this app's service
    names; this applies the rest of the scope and the filters, and shapes what leaves.

    Scope: the service name has already matched (case-insensitively). Here an entry that was ingested under a DIFFERENT app id is
    dropped, so an app that reports under another app's service name cannot show up in that app's reads. Entries from the legacy
    shared secret carry no app id and are kept (that is how SAAPP reported before it had its own client record).

    `level` is a minimum severity (warn means warn and error). Messages are clipped; context goes through the same allowlist as an
    incident's metadata, so a reporting app that attached something large or private to a log line does not leak it through here.
    """
    now = now or datetime.now(timezone.utc)
    if level is not None and level not in LOG_LEVELS:
        raise HTTPException(status_code=400, detail="level must be info, warn or error.")
    if request_id is not None and not _REQUEST_ID_RE.match(request_id):
        raise HTTPException(status_code=400, detail="request_id has an unexpected shape.")
    needle = (contains or "").strip().lower()
    if len(needle) > 80:
        raise HTTPException(status_code=400, detail="contains is limited to 80 characters.")
    start = parse_since(since or DEFAULT_LOG_LOOKBACK, now)
    floor = LOG_LEVELS.get(level or "info", 0)
    limit = max(1, min(int(limit), MAX_LOG_PAGE))

    allowed = list(context["service_names"])
    in_scope = [
        entry for entry in entries
        if str(entry.get("service") or "").strip().lower() in allowed
        and entry.get("source_app_id") in (None, context["app_id"])
    ]
    in_scope.sort(key=lambda entry: str(entry.get("timestamp") or ""))

    matched = []
    for entry in in_scope:
        stamp = _parse_log_time(entry.get("timestamp"))
        if stamp is None or stamp < start:
            continue
        if LOG_LEVELS.get(entry.get("level"), 0) < floor:
            continue
        message = str(entry.get("message") or "")
        if needle and needle not in message.lower():
            continue
        entry_context = entry.get("context") if isinstance(entry.get("context"), dict) else {}
        if request_id is not None and str(entry_context.get("requestId") or "") != request_id:
            continue
        matched.append({
            "time": entry.get("timestamp"),
            "level": entry.get("level"),
            "service": entry.get("service"),
            "message": _clip(message, MAX_LOG_MESSAGE_CHARS),
            "context": sanitize_metadata(entry_context),
        })

    return {
        "entries": matched[-limit:],
        "matched": len(matched),
        "buffered": len(in_scope),
        "oldest_buffered": in_scope[0].get("timestamp") if in_scope else None,
    }


def get_app_incident(db, context: Dict[str, Any], incident_id: str) -> Dict[str, Any]:
    """One incident with its analysis, or 404 for anything outside this app's scope (indistinguishable from "doesn't exist")."""
    query = scope_query(context)
    query["_id"] = incident_id
    incident = db["incidents"].find_one(query)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    analysis = db["analyses"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    remediation = db["remediations"].find_one({"incident_id": incident_id}, sort=[("updated_at", -1), ("created_at", -1)]) or {}
    return sanitize_incident(incident, analysis, remediation, detail=True)
