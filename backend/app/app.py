from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from datetime import datetime

# --- LOG SCHEMAS ---
class LogCreate(BaseModel):
    service_name: str
    environment: str = "production"
    level: str = "ERROR"
    message: str
    stack_trace: str
    metadata: Optional[Dict[str, Any]] = None

# --- AI POST-MORTEM SCHEMA ---
class SuspectCommit(BaseModel):
    commit_sha: str
    author: str
    message: str
    diff_snippet: Optional[str] = None

class PostMortem(BaseModel):
    root_cause: str
    suspect_commit: Optional[SuspectCommit] = None
    suggested_fix: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    generated_at: datetime = Field(default_factory=datetime.utcnow)

# --- INCIDENT SCHEMA ---
class IncidentResponse(BaseModel):
    id: str = Field(alias="_id")
    title: str
    service_name: str
    status: str = "OPEN"
    severity: str = "HIGH"
    occurrences_count: int = 1
    first_seen_at: datetime
    last_seen_at: datetime
    post_mortem: Optional[PostMortem] = None