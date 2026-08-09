# backend/schemas/incident_schemas.py
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from enum import Enum
from pydantic import BaseModel, Field
import logging


class IncidentStatus(str, Enum):
    OPEN = "open"
    ANALYZING = "analyzing"
    FIX_PROPOSED = "fix_proposed"
    RESOLVED = "resolved"
    CLOSED = "closed"


class ActionStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


# 1. INCIDENT DOCUMENT (Raw Error Context)
class IncidentCreate(BaseModel):
    service_name: str = Field(..., example="payment-service")
    environment: str = Field(..., example="production")
    error_message: str = Field(..., example="NullPointerError in checkout process")
    stack_trace: str
    repository: str = Field(..., example="org/repo-name")
    metadata: Optional[Dict[str, Any]] = None


class IncidentInDB(IncidentCreate):
    id: str = Field(..., alias="_id")
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# 2. ANALYSIS DOCUMENT (Populated by AI Squad)
class SuspectCommit(BaseModel):
    commit_sha: str
    author: str
    message: str
    diff_snippet: Optional[str] = None


class RootCauseAnalysis(BaseModel):
    incident_id: str
    root_cause_summary: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    suspect_commits: List[SuspectCommit] = []
    suggested_fix: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# 3. REMEDIATION ACTION DOCUMENT (Human-in-the-Loop PR Flow)
class RemediationAction(BaseModel):
    incident_id: str
    action_type: str = Field(default="create_pull_request")
    target_repo: str
    base_branch: str = "main"
    head_branch: str
    pr_title: str
    pr_body: str
    status: ActionStatus = ActionStatus.PENDING_APPROVAL
    approved_by: Optional[str] = None
    pr_url: Optional[str] = None


# 4. AUDIT LOG DOCUMENT (Backend Ops Ledger)
class AuditLogEntry(BaseModel):
    incident_id: str
    actor: str  # User ID or "SYSTEM_AI"
    action: str  # e.g., "INCIDENT_CREATED", "ANALYSIS_SUBMITTED", "PR_APPROVED"
    details: Dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))