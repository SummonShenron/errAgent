# backend/schemas/ingest_schemas.py
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class MachineIncidentIngest(BaseModel):
    """Request body for POST /api/v1/webhooks/ingest.

    All fields are optional because `ingest_machine_payload`/`_store_incident_and_queue_analysis`
    (backend/utils/app_utils.py) already apply sensible defaults for missing ones — this model
    adds real type validation without narrowing what was previously accepted as a raw dict.
    `extra="allow"` keeps this backward compatible with Sentry-shaped fields (`tags`, `data`,
    `extra`, `project_name`, `logger`) that `_resolve_target_repository` already knows how to
    read from a payload sent directly to this generic endpoint.
    """

    model_config = ConfigDict(extra="allow")

    service_name: Optional[str] = None
    environment: Optional[str] = None
    error_message: Optional[str] = None
    stack_trace: Optional[str] = None
    repository: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
