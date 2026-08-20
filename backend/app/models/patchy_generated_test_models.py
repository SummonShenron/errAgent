from pydantic import BaseModel, Field


class PatchyGeneratedTest(BaseModel):
    test_file: str = Field(description="New regression test path under tests/ or a test_*.py file.")
    test_name: str = Field(description="A pytest test function name beginning with test_.")
    rationale: str = Field(description="How the test reproduces and verifies the incident fix.")
    content: str = Field(description="Complete Python test file content, with no markdown fences.")
