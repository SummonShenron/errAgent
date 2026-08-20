import asyncio

import pytest

import backend.services.patchy_terminal as terminal_module
from backend.services.patchy_test_planner import _validate_test_command, normalize_test_command
from backend.services.log_broker import LogBroker, LogEventInput
from backend.services.patchy_terminal import PatchyCommandError, execute_patchy_command


class FakeCursor(list):
    def sort(self, *_args):
        return self

    def limit(self, count):
        return FakeCursor(self[:count])


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, *_args, **_kwargs):
        return FakeCursor(self.documents)

    def find_one(self, query, **_kwargs):
        for document in reversed(self.documents):
            matched = True
            for key, value in query.items():
                if isinstance(value, dict) and "$ne" in value:
                    if document.get(key) == value["$ne"]:
                        matched = False
                        break
                elif document.get(key) != value:
                    matched = False
                    break
            if matched:
                return document
        return None


class FakeDB:
    def __init__(self):
        self.collections = {
            "incidents": FakeCollection([
                {"_id": "inc_1", "service_name": "SAAPP", "status": "open", "error_message": "Workflow failed"},
                {"_id": "inc_resolved", "service_name": "BTY", "status": "resolved", "error_message": "Old failure"},
            ]),
            "analyses": FakeCollection([
                {"incident_id": "inc_1", "root_cause_summary": "A dependency timed out", "suggested_fix": "Retry safely"},
                {"incident_id": "inc_resolved", "root_cause_summary": "A bug was fixed", "suggested_fix": "Already merged"},
            ]),
            "remediations": FakeCollection([
                {"_id": "rem_1", "incident_id": "inc_1", "status": "merged"},
            ]),
            "patchy_test_plans": FakeCollection([]),
            "patchy_plans": FakeCollection([]),
            "patchy_proposals": FakeCollection([]),
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_patchy_commands_are_allowlisted_and_structured(monkeypatch):
    monkeypatch.setattr(
        terminal_module,
        "run_service_health_checks",
        lambda: [
            {"service": "BTY Fitness", "status": "healthy", "latency_ms": 120, "http_status": 200, "details": {}},
            {"service": "SAAPP Widget", "status": "down", "latency_ms": None, "http_status": 503, "details": {}},
        ],
    )

    async def scenario():
        broker = LogBroker()
        await broker.publish(LogEventInput(service="SAAPP", level="error", message="Workflow failed"))
        db = FakeDB()

        help_result = await execute_patchy_command("help", db, broker)
        assert help_result["status"] == "success"
        assert any("diagnostics" in line for line in help_result["lines"])

        health_result = await execute_patchy_command("health bty", db, broker)
        assert health_result["title"] == "Health check: OK"
        assert len(health_result["data"]["services"]) == 1

        incidents_result = await execute_patchy_command("incidents", db, broker)
        assert incidents_result["status"] == "warning"
        assert "inc_1" in incidents_result["lines"][0]

        all_incidents_result = await execute_patchy_command("list incidents all", db, broker)
        assert all_incidents_result["title"] == "All incidents (2)"
        assert {item["_id"] for item in all_incidents_result["data"]} == {"inc_1", "inc_resolved"}

        open_incidents_result = await execute_patchy_command("list incidents open", db, broker)
        assert open_incidents_result["title"] == "Open incidents (1)"
        assert open_incidents_result["data"][0]["_id"] == "inc_1"

        resolved_incidents_result = await execute_patchy_command("list incidents resolved", db, broker)
        assert resolved_incidents_result["title"] == "Resolved incidents (1)"
        assert resolved_incidents_result["data"][0]["_id"] == "inc_resolved"

        logs_result = await execute_patchy_command("logs saapp error", db, broker)
        assert logs_result["data"][0]["message"] == "Workflow failed"

        clarification_result = await execute_patchy_command("probe", db, broker)
        assert clarification_result["status"] == "clarification_required"
        assert [option["value"] for option in clarification_result["data"]["clarification"]["options"]] == [
            "probe bty",
            "probe saapp",
        ]
        assert not db["patchy_proposals"].documents

        synthetic_result = await execute_patchy_command("synthetic bty", db, broker)
        assert synthetic_result["status"] == "approval_required"
        assert synthetic_result["data"]["proposal"]["kind"] == "synthetic_http"

        investigation_clarification = await execute_patchy_command("investigate", db, broker)
        assert investigation_clarification["status"] == "clarification_required"
        assert investigation_clarification["data"]["clarification"]["options"][0]["value"] == "investigate inc_1"

        explain_result = await execute_patchy_command("explain inc_1", db, broker)
        assert "A dependency timed out" in explain_result["lines"][3]

        diagnostics_result = await execute_patchy_command("diagnostics", db, broker)
        assert diagnostics_result["status"] == "warning"

        ops_result = await execute_patchy_command("ops status saapp", db, broker)
        assert ops_result["status"] == "warning"
        assert ops_result["data"]["services"][0]["alias"] == "saapp"
        assert "Active incidents: 1" in ops_result["lines"]

        async def fake_render_status(target):
            return {
                "provider": "render",
                "target": target,
                "status": "ok",
                "services": [{
                    "alias": "saapp",
                    "serviceId": "srv_saapp",
                    "status": "ok",
                    "service": {"name": "SAAPP Widget", "suspended": False},
                    "latestDeploy": {"status": "live", "commit": "abc123", "id": "dep_1", "finishedAt": "2026-08-19T15:00:00Z"},
                }],
            }

        monkeypatch.setattr(terminal_module, "collect_render_status", fake_render_status)
        render_result = await execute_patchy_command("render status saapp", db, broker)
        assert render_result["status"] == "success"
        assert "Latest deploy: live | commit=abc123" in render_result["lines"]

        async def fake_synthesis(incident_id, _db, _broker):
            return {
                "incidentId": incident_id,
                "synthesis": {
                    "summary": "The dependency timeout is the leading explanation.",
                    "hypotheses": [{"claim": "A dependency timed out.", "confidence": 0.78, "evidence": ["incident error"]}],
                    "missing_information": ["Dependency endpoint"],
                    "recommended_action": "Run service error-log review",
                    "recommended_command": "logs saapp error",
                    "should_ask_operator": True,
                },
                "evidence": {"errorLogCount": 1, "productionStatus": {}},
            }

        monkeypatch.setattr(terminal_module, "synthesize_incident", fake_synthesis)
        synthesis_result = await execute_patchy_command("summarize inc_1", db, broker)
        assert synthesis_result["title"] == "Patchy evidence synthesis"
        assert "dependency timeout" in synthesis_result["lines"][0]
        assert any("Missing information" in line for line in synthesis_result["lines"])

        confirmation = await execute_patchy_command("confirm deployed inc_1", db, broker, actor="operator")
        assert confirmation["status"] == "success"
        assert db["remediations"].documents["rem_1"]["deployment_status"] == "confirmed"

        with pytest.raises(PatchyCommandError, match="Unknown command"):
            await execute_patchy_command("rm -rf /", db, broker)

    asyncio.run(scenario())


def test_test_command_validation_accepts_pytest_nodes_and_rejects_shell_text():
    test_files = {"test_main.py"}
    assert _validate_test_command("pytest test_main.py", test_files)
    assert normalize_test_command("pytest test_main.py") == "python -m pytest test_main.py"
    assert _validate_test_command("python -m pytest test_main.py", test_files)
    assert _validate_test_command("pytest test_main.py::TestDownload::test_unicode_filename", test_files)
    assert not _validate_test_command("pytest test_main.py -q", test_files)
    assert not _validate_test_command("pytest test_main.py && whoami", test_files)
    assert not _validate_test_command("pytest ../test_main.py", test_files)


def test_investigate_handles_active_and_resolved_incidents(monkeypatch):
    monkeypatch.setattr(
        terminal_module,
        "run_service_health_checks",
        lambda: [
            {"service": "BTY Fitness", "status": "healthy", "latency_ms": 120, "http_status": 200, "details": {}},
            {"service": "SAAPP Widget", "status": "healthy", "latency_ms": 130, "http_status": 200, "details": {}},
        ],
    )

    async def scenario():
        broker = LogBroker()
        db = FakeDB()

        active_plan = await execute_patchy_command("investigate inc_1", db, broker, actor="operator")
        assert active_plan["data"]["plan"]["kind"] == "incident_investigation"
        assert [step["command"] for step in active_plan["data"]["plan"]["steps"]] == [
            "explain inc_1",
        ]

        next_result = await execute_patchy_command("next", db, broker, actor="operator")
        assert next_result["data"]["plan"]["steps"][1]["command"] == "logs saapp error"
        completed = await execute_patchy_command("next", db, broker, actor="operator")
        assert completed["data"]["planReport"]["title"] == "Investigation complete: inc_1"

        resolved_plan = await execute_patchy_command("investigate inc_resolved", db, broker, actor="operator")
        assert resolved_plan["data"]["plan"]["kind"] == "resolution_review"
        assert len(resolved_plan["data"]["plan"]["steps"]) == 1

    asyncio.run(scenario())


def test_test_plan_is_read_only_and_validates_pytest_targets(monkeypatch):
    class FakeGitHub:
        async def fetch_repository_context(self, repo, branch):
            assert repo == "SummonShenron/SAAPP"
            assert branch == "main"
            return {"branch": branch, "testFiles": ["tests/test_downloads.py"]}

        async def fetch_repository_files(self, repo, branch, paths):
            return {paths[0]: "def test_unicode_filename():\n    pass\n"}

    class FakePlan:
        summary = "Cover the Unicode filename response path."
        recommendations = [
            type("Recommendation", (), {
                "command": "pytest tests/test_downloads.py::test_unicode_filename",
                "confidence": 0.93,
                "rationale": "Matches the StreamingResponse header failure.",
                "model_dump": lambda self: {
                    "test_file": "tests/test_downloads.py",
                    "test_name": "test_unicode_filename",
                    "rationale": self.rationale,
                    "command": self.command,
                    "confidence": self.confidence,
                },
            })(),
        ]
        missing_information = []

        def model_dump(self):
            return {
                "summary": self.summary,
                "recommendations": [item.model_dump() for item in self.recommendations],
                "missing_information": self.missing_information,
                "should_ask_operator": False,
            }

    async def scenario():
        db = FakeDB()
        db["incidents"].documents[0]["repository"] = "SummonShenron/SAAPP"
        monkeypatch.setattr(terminal_module, "GitHubOpsService", FakeGitHub)
        async def fake_create_test_plan(incident_id, _db, _github):
            return {
                "incidentId": incident_id,
                "repository": "SummonShenron/SAAPP",
                "branch": "main",
                "plan": FakePlan().model_dump(),
                "repositoryContext": {"testFiles": ["tests/test_downloads.py"], "fetchedFileCount": 1},
            }

        monkeypatch.setattr(terminal_module, "create_test_plan", fake_create_test_plan)
        result = await execute_patchy_command("test plan inc_1", db, LogBroker())
        assert result["title"] == "Patchy test plan"
        assert "pytest tests/test_downloads.py::test_unicode_filename" in result["lines"]

    asyncio.run(scenario())


def test_patchy_plan_creates_and_advances_allowlisted_steps(monkeypatch):
    monkeypatch.setattr(
        terminal_module,
        "run_service_health_checks",
        lambda: [
            {"service": "BTY Fitness", "status": "healthy", "latency_ms": 120, "http_status": 200, "details": {}},
            {"service": "SAAPP Widget", "status": "healthy", "latency_ms": 130, "http_status": 200, "details": {}},
        ],
    )

    async def scenario():
        broker = LogBroker()
        db = FakeDB()

        plan_result = await execute_patchy_command("plan verify bty stability", db, broker, actor="operator")
        assert plan_result["status"] == "success"
        plan_id = plan_result["data"]["plan"]["_id"]
        assert plan_result["data"]["plan"]["steps"][0]["command"] == "verify bty"

        next_result = await execute_patchy_command("next", db, broker, actor="operator")
        assert next_result["status"] == "approval_required"
        assert next_result["data"]["stepResult"]["data"]["proposal"]["workflow"]["goal"] == "Verify BTY Fitness stability"
        assert next_result["data"]["plan"]["steps"][0]["status"] == "completed"
        assert "Next: next" in next_result["lines"]

        with pytest.raises(PatchyCommandError, match="Known plans"):
            await execute_patchy_command("plan run powershell", db, broker, actor="operator")

    asyncio.run(scenario())
