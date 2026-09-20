"""Covers the investigation-loop wiring added to the incident-analysis pipeline: that
``_run_analysis_with_source`` enriches its final prompt with investigation context when given
tools, behaves exactly as before when it isn't, and that ``run_ai_analysis_pipeline`` (the
production path) constructs tools when a team's GitHub credential is available and gracefully
degrades when it isn't — while ``run_ai_analysis_pipeline_local`` never does either.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.utils import app_utils


class FakeCollection:
    def __init__(self):
        self.documents = {}
        self._auto_id = 0

    def insert_one(self, document):
        self._auto_id += 1
        key = document.get("_id", self._auto_id)
        self.documents[key] = deepcopy(document)

    def find_one(self, query, projection=None, sort=None):
        matches = [doc for doc in self.documents.values() if self._matches(doc, query)]
        if not matches:
            return None
        if sort:
            field, direction = sort[0]
            matches.sort(key=lambda d: d.get(field) or 0, reverse=direction < 0)
        return deepcopy(matches[0])

    def update_one(self, query, update, upsert=False):
        for doc in self.documents.values():
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return type("Result", (), {"modified_count": 1})()
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$setOnInsert", {}))
            new_doc.update(update.get("$set", {}))
            self.insert_one(new_doc)
            return type("Result", (), {"modified_count": 0, "upserted_id": new_doc.get("_id")})()
        return type("Result", (), {"modified_count": 0})()

    @staticmethod
    def _matches(doc, query):
        for key, value in query.items():
            if doc.get(key) != value:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.collections = {
            "incidents": FakeCollection(),
            "remediations": FakeCollection(),
            "analyses": FakeCollection(),
            "teams": FakeCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


class _FakeParsedResult:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class _FakeTextResponse:
    def __init__(self, text):
        self.text = text


class _FakeParsedResponse:
    def __init__(self, parsed):
        self.parsed = parsed


class _FakeModels:
    def __init__(self, outer):
        self._outer = outer

    def generate_content(self, model, contents, config):
        self._outer.calls.append({"model": model, "contents": contents, "config": config})
        if getattr(config, "response_schema", None) is not None:
            return _FakeParsedResponse(self._outer.final_result)
        return _FakeTextResponse(self._outer.step_texts.pop(0))


class _FakeGeminiClient:
    """Distinguishes investigation-step calls (freeform text, no response_schema) from the
    final structured patch call (response_schema=AIAnalysisSchema) purely by inspecting the
    config each call was made with — the same distinction _run_analysis_with_source makes."""

    def __init__(self, step_texts, final_result):
        self.step_texts = list(step_texts)
        self.final_result = final_result
        self.calls = []
        self.models = _FakeModels(self)


def _final_result(**overrides) -> _FakeParsedResult:
    fields = dict(
        root_cause_summary="A value from config.py is None at request time.",
        severity="MEDIUM",
        suggested_fix="Default the value before use.",
        code_patch="",
        old_snippet="value = None",
        new_snippet="value = get_default()",
        head_branch="fix/config-default",
        base_branch="main",
        pr_title="Fix missing default",
        pr_body="Adds a default so the value is never None.",
    )
    fields.update(overrides)
    return _FakeParsedResult(**fields)


@pytest.fixture(autouse=True)
def _stub_patch_synthesis(monkeypatch):
    # _run_analysis_with_source's downstream (snippet->diff synthesis, safety validation, and
    # the git-backed sandbox apply) is pre-existing, untouched, and already shells out to a real
    # git binary — irrelevant to what this test file covers (the investigation wiring), so it's
    # stubbed to a fixed, always-valid outcome rather than exercised for real here.
    monkeypatch.setattr(app_utils, "_build_patch_from_snippet_edit", lambda **kwargs: "--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n")
    monkeypatch.setattr(app_utils, "_validate_patch_safety", lambda **kwargs: None)
    monkeypatch.setattr(
        app_utils, "_apply_patch_in_sandbox", lambda **kwargs: ("value = get_default()\n", "--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n")
    )


def _base_payload():
    return {
        "service_name": "widget-api",
        "environment": "production",
        "stack_trace": 'File "app.py", line 10, in handler\n    use(value)',
    }


def test_run_analysis_without_investigation_tools_makes_exactly_one_gemini_call():
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-1", "team_id": "team-a"})
    client = _FakeGeminiClient(step_texts=[], final_result=_final_result())

    app_utils._run_analysis_with_source(
        db, client, "inc-1", _base_payload(), "app.py", "value = None\n",
        remediation_extra={"target_repo": "org/repo"},
        investigation_tools=None,
    )

    assert len(client.calls) == 1
    assert getattr(client.calls[0]["config"], "response_schema", None) is not None
    # No investigation section should be injected when no tools were given.
    assert "ADDITIONAL CONTEXT GATHERED BY INVESTIGATION" not in client.calls[0]["contents"]


def test_run_analysis_with_investigation_tools_runs_the_loop_and_enriches_the_final_prompt():
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-2", "team_id": "team-a"})
    client = _FakeGeminiClient(
        step_texts=[
            '{"action": "query", "purpose": "check the caller", "tool_action": "read_repo_file", "args": {"path": "caller.py"}}',
            '{"action": "final"}',
        ],
        final_result=_final_result(),
    )

    def act(tool_action, args):
        assert tool_action == "read_repo_file"
        assert args == {"path": "caller.py"}
        return "def caller():\n    use(value=None)  # <- root cause is here"

    tools = app_utils.InvestigationTools(actions_menu="- read_repo_file — args: path", act=act)

    app_utils._run_analysis_with_source(
        db, client, "inc-2", _base_payload(), "app.py", "value = None\n",
        remediation_extra={"target_repo": "org/repo"},
        investigation_tools=tools,
    )

    # Two investigation-step calls (query, final) plus the one final structured patch call.
    assert len(client.calls) == 3
    final_call = client.calls[-1]
    assert getattr(final_call["config"], "response_schema", None) is not None
    assert "root cause is here" in final_call["contents"]
    assert "ADDITIONAL CONTEXT GATHERED BY INVESTIGATION" in final_call["contents"]


def test_run_analysis_investigation_failure_degrades_to_single_file_analysis(monkeypatch):
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-3", "team_id": "team-a"})
    client = _FakeGeminiClient(step_texts=[], final_result=_final_result())

    def _broken_act(tool_action, args):
        raise RuntimeError("network exploded")

    tools = app_utils.InvestigationTools(actions_menu="- read_repo_file — args: path", act=_broken_act)

    # The loop itself catches per-tool exceptions, but simulate the whole loop raising (e.g. a
    # bug in the loop) to confirm _run_analysis_with_source never lets that take down analysis.
    monkeypatch.setattr(
        app_utils, "run_investigation_loop", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("loop exploded"))
    )

    app_utils._run_analysis_with_source(
        db, client, "inc-3", _base_payload(), "app.py", "value = None\n",
        remediation_extra={"target_repo": "org/repo"},
        investigation_tools=tools,
    )

    assert len(client.calls) == 1
    assert "ADDITIONAL CONTEXT GATHERED BY INVESTIGATION" not in client.calls[0]["contents"]


def test_run_ai_analysis_pipeline_passes_investigation_tools_when_team_has_github_credential(monkeypatch):
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-4", "team_id": "team-a"})
    monkeypatch.setattr(app_utils, "get_db", lambda: db)
    monkeypatch.setattr(app_utils.os, "getenv", lambda key, default=None: "fake-api-key" if key == "GOOGLE_API_KEY" else default)
    monkeypatch.setattr(app_utils.genai, "Client", lambda api_key: object())
    monkeypatch.setattr(app_utils, "_extract_target_file_candidates", lambda stack_trace, payload: ["app.py"])

    class _FakeUrlResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"value = None\n"

    monkeypatch.setattr(app_utils.urllib.request, "urlopen", lambda req: _FakeUrlResponse())
    monkeypatch.setattr(app_utils, "get_github_service_for_team", lambda db, team_id: object())

    captured = {}

    def _fake_run_analysis_with_source(db, client, incident_id, payload, target_file, existing_code, remediation_extra, investigation_tools=None):
        captured["investigation_tools"] = investigation_tools
        return {}

    monkeypatch.setattr(app_utils, "_run_analysis_with_source", _fake_run_analysis_with_source)

    app_utils.run_ai_analysis_pipeline("inc-4", _base_payload())

    assert captured["investigation_tools"] is not None


def test_run_ai_analysis_pipeline_degrades_when_team_has_no_github_credential(monkeypatch):
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-5", "team_id": "team-b"})
    monkeypatch.setattr(app_utils, "get_db", lambda: db)
    monkeypatch.setattr(app_utils.os, "getenv", lambda key, default=None: "fake-api-key" if key == "GOOGLE_API_KEY" else default)
    monkeypatch.setattr(app_utils.genai, "Client", lambda api_key: object())
    monkeypatch.setattr(app_utils, "_extract_target_file_candidates", lambda stack_trace, payload: ["app.py"])

    class _FakeUrlResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"value = None\n"

    monkeypatch.setattr(app_utils.urllib.request, "urlopen", lambda req: _FakeUrlResponse())

    def _raise_no_credential(db, team_id):
        raise HTTPException(status_code=503, detail="not configured")

    monkeypatch.setattr(app_utils, "get_github_service_for_team", _raise_no_credential)

    captured = {}

    def _fake_run_analysis_with_source(db, client, incident_id, payload, target_file, existing_code, remediation_extra, investigation_tools=None):
        captured["investigation_tools"] = investigation_tools
        return {}

    monkeypatch.setattr(app_utils, "_run_analysis_with_source", _fake_run_analysis_with_source)

    app_utils.run_ai_analysis_pipeline("inc-5", _base_payload())

    assert captured["investigation_tools"] is None


def test_run_ai_analysis_pipeline_local_never_passes_investigation_tools(monkeypatch):
    import backend.services.patchy_local_patch as patchy_local_patch

    db = FakeDB()
    monkeypatch.setattr(app_utils, "get_db", lambda: db)
    monkeypatch.setattr(app_utils.os, "getenv", lambda key, default=None: "fake-api-key" if key == "GOOGLE_API_KEY" else default)
    monkeypatch.setattr(app_utils.genai, "Client", lambda api_key: object())
    monkeypatch.setattr(patchy_local_patch, "create_local_patch_proposal", lambda db, incident_id, remediation_doc: None)

    captured = {}

    def _fake_run_analysis_with_source(db, client, incident_id, payload, target_file, existing_code, remediation_extra, investigation_tools=None):
        captured["investigation_tools"] = investigation_tools
        return {}

    monkeypatch.setattr(app_utils, "_run_analysis_with_source", _fake_run_analysis_with_source)

    payload = {
        "service_name": "local-app",
        "stack_trace": 'File "app.py", line 1',
        "metadata": {
            "target_file_path": "app.py",
            "inline_file_content": "value = None\n",
            "local_project_root": "/tmp/local-app",
        },
    }
    app_utils.run_ai_analysis_pipeline_local("inc-local-1", payload)

    assert captured["investigation_tools"] is None


def _freetext_payload(description="the login page isn't redirecting after signup"):
    return {
        "service_name": "local-app",
        "stack_trace": "",
        "metadata": {
            "local_dev": True,
            "user_reported_description": description,
            "local_project_root": "/home/dev/local-app",
        },
    }


def test_run_ai_analysis_pipeline_local_investigate_locates_file_via_local_bridge(monkeypatch):
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-ft-1", "team_id": "team-a"})
    monkeypatch.setattr(app_utils, "get_db", lambda: db)
    monkeypatch.setattr(app_utils.os, "getenv", lambda key, default=None: "fake-api-key" if key == "GOOGLE_API_KEY" else default)

    client = _FakeGeminiClient(
        step_texts=[
            # Pass 1 (run_ai_analysis_pipeline_local_investigate): locate the file.
            json.dumps({"action": "query", "purpose": "see the project", "tool_action": "list_local_tree", "args": {}}),
            json.dumps({"action": "query", "purpose": "read the signup handler", "tool_action": "read_local_file", "args": {"path": "signup.py"}}),
            json.dumps({"action": "final"}),
            # Pass 2 (inside _run_analysis_with_source): gather more context; nothing more needed here.
            json.dumps({"action": "final"}),
        ],
        final_result=_final_result(),
    )
    monkeypatch.setattr(app_utils.genai, "Client", lambda api_key: client)

    tool_calls = []

    def fake_tools_builder(db_arg, incident_id, poll_interval=1.0, poll_timeout=30.0):
        def act(tool_action, args):
            tool_calls.append((tool_action, args))
            if tool_action == "list_local_tree":
                return "signup.py\nother.py"
            if tool_action == "read_local_file":
                return "def handle_signup(): ...\nredirect = None"
            return f"ERROR: unrecognized tool_action '{tool_action}'"

        return app_utils.InvestigationTools(actions_menu="- list_local_tree\n- read_local_file", act=act)

    monkeypatch.setattr(app_utils, "build_local_bridge_investigation_tools", fake_tools_builder)
    monkeypatch.setattr("backend.services.patchy_local_patch.create_local_patch_proposal", lambda db, incident_id, remediation_doc: None)

    app_utils.run_ai_analysis_pipeline_local_investigate("inc-ft-1", _freetext_payload())

    remediation = db["remediations"].find_one({"incident_id": "inc-ft-1"})
    assert remediation is not None
    assert remediation["target_file_path"] == "signup.py"
    assert db["incidents"].find_one({"_id": "inc-ft-1"})["status"] == "fix_proposed"
    assert ("read_local_file", {"path": "signup.py"}) in tool_calls


def test_run_ai_analysis_pipeline_local_investigate_fails_when_no_file_is_ever_read(monkeypatch):
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-ft-2", "team_id": "team-a"})
    monkeypatch.setattr(app_utils, "get_db", lambda: db)
    monkeypatch.setattr(app_utils.os, "getenv", lambda key, default=None: "fake-api-key" if key == "GOOGLE_API_KEY" else default)

    # Model concludes immediately without ever reading a real file.
    client = _FakeGeminiClient(step_texts=[json.dumps({"action": "final"})], final_result=_final_result())
    monkeypatch.setattr(app_utils.genai, "Client", lambda api_key: client)

    def fake_tools_builder(db_arg, incident_id, poll_interval=1.0, poll_timeout=30.0):
        return app_utils.InvestigationTools(actions_menu="- list_local_tree\n- read_local_file", act=lambda a, k: "unused")

    monkeypatch.setattr(app_utils, "build_local_bridge_investigation_tools", fake_tools_builder)

    app_utils.run_ai_analysis_pipeline_local_investigate("inc-ft-2", _freetext_payload())

    assert db["incidents"].find_one({"_id": "inc-ft-2"})["status"] == "analysis_failed"
    remediation = db["remediations"].find_one({"incident_id": "inc-ft-2"})
    assert "could not identify a specific file" in remediation["failure_reason"].lower()
    assert remediation.get("content_source") != "sandbox_applied"


def test_run_ai_analysis_pipeline_local_investigate_requires_a_description(monkeypatch):
    db = FakeDB()
    db["incidents"].insert_one({"_id": "inc-ft-3", "team_id": "team-a"})
    monkeypatch.setattr(app_utils, "get_db", lambda: db)

    payload = _freetext_payload(description="")
    app_utils.run_ai_analysis_pipeline_local_investigate("inc-ft-3", payload)

    assert db["incidents"].find_one({"_id": "inc-ft-3"})["status"] == "analysis_failed"
