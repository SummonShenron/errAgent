import asyncio

from backend.services import circuit_architect


def test_debug_run_uses_latest_run_evidence(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured["prompt"] = kwargs["contents"]
            return type("Response", (), {
                "parsed": {
                    "status": "proposal",
                    "summary": "The transform failed before producing jobs.",
                    "findings": [{"severity": "error", "node_id": "transform_123"}],
                    "patch": {"update_nodes": [], "add_nodes": [], "add_edges": []},
                    "requires_approval": True,
                    "errors": [],
                }
            })()

    class FakeClient:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(circuit_architect.genai, "Client", FakeClient)
    request = circuit_architect.CircuitArchitectRequest(
        operation="debug_run",
        goal="Explain the failure",
        context={
            "latest_run": {
                "errors": ["missing jobs"],
                "logs": ["transform started"],
                "trace": ["transform_123"],
                "outputs": {},
            }
        },
    )

    result = asyncio.run(circuit_architect.create_circuit_architect_response(request))

    assert result.status == "proposal"
    assert "missing jobs" in captured["prompt"]
    assert result.patch == {"update_nodes": [], "add_nodes": [], "add_edges": []}


def test_missing_gemini_key_returns_structured_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    request = circuit_architect.CircuitArchitectRequest(operation="plan_workflow", goal="Plan it")

    result = asyncio.run(circuit_architect.create_circuit_architect_response(request))

    assert result.status == "error"
    assert "GOOGLE_API_KEY" in result.errors[0]


def test_plan_workflow_rejects_unavailable_node_type():
    try:
        circuit_architect._validate_patch(
            "plan_workflow",
            {"add_inputs": [], "add_nodes": [{"type": "Unknown"}], "add_edges": []},
            ["Transform", "LLM"],
        )
    except circuit_architect.CircuitArchitectError as exc:
        assert "not available" in str(exc)
    else:
        raise AssertionError("Unavailable node type was accepted")


def test_patch_workflow_rejects_removals():
    try:
        circuit_architect._validate_patch(
            "patch_workflow",
            {"update_nodes": [], "remove_nodes": ["dangerous-node"]},
        )
    except circuit_architect.CircuitArchitectError as exc:
        assert "may not remove" in str(exc)
    else:
        raise AssertionError("Removal patch was accepted")