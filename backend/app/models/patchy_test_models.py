from pydantic import BaseModel, Field


class PatchyTestRecommendation(BaseModel):
    test_file: str = Field(description="Existing repository test file to inspect or extend.")
    test_name: str = Field(description="Specific existing test or proposed test name.")
    rationale: str = Field(description="Why this test covers the incident evidence.")
    command: str = Field(description="A safe pytest command using only the selected test file or test name.")
    confidence: float = Field(ge=0, le=1, description="Confidence that this test is relevant.")


class PatchyTestPlan(BaseModel):
    summary: str = Field(description="Concise test strategy for the incident.")
    recommendations: list[PatchyTestRecommendation] = Field(default_factory=list, max_length=5)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    should_ask_operator: bool = Field(description="Whether a missing repository or test detail blocks planning.")
