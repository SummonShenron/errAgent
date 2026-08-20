from pydantic import BaseModel, Field


class EvidenceHypothesis(BaseModel):
    claim: str = Field(description="A concise, evidence-based hypothesis.")
    confidence: float = Field(ge=0, le=1, description="Confidence from 0 to 1.")
    evidence: list[str] = Field(default_factory=list, description="Short references to supplied evidence.")


class PatchyIncidentSynthesis(BaseModel):
    summary: str = Field(description="A concise operational summary of the incident.")
    hypotheses: list[EvidenceHypothesis] = Field(default_factory=list, max_length=5)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    recommended_action: str = Field(description="One safe next action using the supplied allowlisted command vocabulary.")
    recommended_command: str = Field(description="One of: explain, logs, diagnostics, ops status, or none.")
    should_ask_operator: bool = Field(description="Whether missing information should be requested before continuing.")
