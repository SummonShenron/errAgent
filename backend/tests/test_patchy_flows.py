import asyncio
from copy import deepcopy

import pytest

import backend.services.patchy_flow_runner as flow_module
from backend.services.patchy_flow_runner import (
    PatchyFlowError,
    create_flow_plan,
    create_flow_proposal,
    create_validation_proposal,
    execute_flow,
    list_flow_plans,
)
from backend.services.patchy_terminal import _parse_compact_flow_actions


class FakeResult:
    def __init__(self, modified_count=0):
        self.modified_count = modified_count


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = {doc["_id"]: deepcopy(doc) for doc in (documents or [])}

    def insert_one(self, document):
        self.documents[document["_id"]] = deepcopy(document)

    def find_one(self, query, **_kwargs):
        if "_id" in query:
            document = self.documents.get(query["_id"])
            return deepcopy(document) if document else None
        for document in self.documents.values():
            if all(document.get(key) == value for key, value in query.items()):
                return deepcopy(document)
        return None

    def find(self, query=None, **_kwargs):
        query = query or {}
        matches = [
            deepcopy(document)
            for document in self.documents.values()
            if all(document.get(key) == value for key, value in query.items())
        ]

        class Cursor(list):
            def sort(self, *_args):
                return self

            def limit(self, count):
                return Cursor(self[:count])

        return Cursor(matches)

    def update_one(self, query, update, upsert=False):
        document = self.documents.get(query.get("_id"))
        if not document:
            if upsert:
                document = dict(update.get("$setOnInsert", {}))
                self.documents[document["_id"]] = document
            return FakeResult()
        expected_status = query.get("status")
        if expected_status and document.get("status") != expected_status:
            return FakeResult()
        document.update(update.get("$set", {}))
        return FakeResult(modified_count=1)

    def count_documents(self, query):
        return len([doc for doc in self.documents.values() if all(doc.get(k) == v for k, v in query.items())])


class FakeDB:
    def __init__(self):
        self.collections = {
            "patchy_flow_plans": FakeCollection(),
            "patchy_flow_runs": FakeCollection(),
            "patchy_proposals": FakeCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None, cookies=None, elapsed_ms=42):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}
        self.cookies = cookies or {}
        from datetime import timedelta
        self.elapsed = timedelta(milliseconds=elapsed_ms)

    def json(self):
        if self._json_data is None:
            raise ValueError("not json")
        return self._json_data


class FakeAsyncClient:
    responses = []
    captured_headers = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def request(self, method, url, json=None, headers=None):
        FakeAsyncClient.captured_headers.append(headers)
        return FakeAsyncClient.responses.pop(0)


def _patch_client(monkeypatch, responses):
    FakeAsyncClient.responses = list(responses)
    monkeypatch.setattr(flow_module.httpx, "AsyncClient", FakeAsyncClient)


def test_flow_plan_validation():
    db = FakeDB()

    with pytest.raises(PatchyFlowError, match="Unknown service alias"):
        create_flow_plan("unknown", "x", [{"type": "GET", "url": "/a"}], "op", db)

    with pytest.raises(PatchyFlowError, match="at least one action"):
        create_flow_plan("bty", "x", [], "op", db)

    with pytest.raises(PatchyFlowError, match="site-relative path"):
        create_flow_plan("bty", "x", [{"type": "GET", "url": "https://evil.com"}], "op", db)

    with pytest.raises(PatchyFlowError, match="unsupported type"):
        create_flow_plan("bty", "x", [{"type": "SHELL", "url": "/a"}], "op", db)

    with pytest.raises(PatchyFlowError, match="at least one HTTP request"):
        create_flow_plan("bty", "x", [{"type": "assert_status", "equals": 200}], "op", db)

    flow = create_flow_plan(
        "bty",
        "signup flow",
        [
            {"type": "GET", "url": "/signup"},
            {"type": "POST", "url": "/api/signup", "body": {"email": "a@b.co"}, "expect_status": 201, "capture": {"uid": "json.userId"}},
            {"type": "assert_status", "equals": 201},
        ],
        "op",
        db,
    )
    assert flow["status"] == "ready"
    assert flow["service"] == "BTY Fitness"
    assert len(flow["steps"]) == 3
    assert list_flow_plans(db, "bty")[0]["_id"] == flow["_id"]


def test_flow_proposal_and_execution_success(monkeypatch):
    _patch_client(monkeypatch, [
        FakeResponse(200, text="<html>signup</html>"),
        FakeResponse(201, json_data={"userId": "u_123"}),
        FakeResponse(200, json_data={"ok": True, "user": "u_123"}),
    ])

    async def scenario():
        db = FakeDB()
        flow = create_flow_plan(
            "bty",
            "signup",
            [
                {"type": "GET", "url": "/signup"},
                {"type": "POST", "url": "/api/signup", "body": {"email": "a@b.co"}, "expect_status": 201, "capture": {"uid": "json.userId"}},
                {"type": "GET", "url": "/api/users/{{uid}}"},
            ],
            "op",
            db,
        )
        proposal = create_flow_proposal(flow["_id"], "op", db)
        assert proposal["status"] == "awaiting_approval"
        assert proposal["kind"] == "synthetic_flow"

        completed = await execute_flow(db, proposal["_id"], "approver")
        assert completed["status"] == "succeeded"
        assert completed["result"]["stepsPassed"] == 3
        assert completed["result"]["steps"][1]["httpStatus"] == 201

        updated_flow = db["patchy_flow_plans"].find_one({"_id": flow["_id"]})
        assert updated_flow["status"] == "passed"

    asyncio.run(scenario())


def test_flow_stops_at_first_failure(monkeypatch):
    _patch_client(monkeypatch, [
        FakeResponse(500, text="boom"),
        FakeResponse(200),
    ])

    async def scenario():
        db = FakeDB()
        flow = create_flow_plan(
            "saapp",
            "login",
            [
                {"type": "GET", "url": "/login", "expect_status": 200},
                {"type": "GET", "url": "/dashboard"},
            ],
            "op",
            db,
        )
        proposal = create_flow_proposal(flow["_id"], "op", db)
        completed = await execute_flow(db, proposal["_id"], "approver")
        assert completed["status"] == "failed"
        assert "expected 200, got 500" in completed["result"]["failure"]
        assert len(completed["result"]["steps"]) == 1  # stopped before step 2

    asyncio.run(scenario())


def test_flow_assert_json_and_template_errors(monkeypatch):
    _patch_client(monkeypatch, [FakeResponse(200, json_data={"token": "abc"})])

    async def scenario():
        db = FakeDB()
        flow = create_flow_plan(
            "bty",
            "assert json",
            [
                {"type": "GET", "url": "/api/session"},
                {"type": "assert_json", "has": "token"},
                {"type": "assert_json", "equals": {"token": "abc"}},
            ],
            "op",
            db,
        )
        proposal = create_flow_proposal(flow["_id"], "op", db)
        completed = await execute_flow(db, proposal["_id"], "approver")
        assert completed["status"] == "succeeded"
        assert completed["result"]["stepsPassed"] == 3

    asyncio.run(scenario())


def test_flow_requires_approval():
    async def scenario():
        db = FakeDB()
        flow = create_flow_plan("bty", "x", [{"type": "GET", "url": "/a"}], "op", db)
        proposal = create_flow_proposal(flow["_id"], "op", db)
        await execute_flow(db, proposal["_id"], "approver")
        with pytest.raises(PatchyFlowError, match="cannot be approved"):
            await execute_flow(db, proposal["_id"], "approver")

    asyncio.run(scenario())


def test_flow_env_bearer_auth_injects_header(monkeypatch):
    _patch_client(monkeypatch, [FakeResponse(200, json_data={"status": "ok"})])
    monkeypatch.setenv("ERRAGENT_BTY_ADMIN_TOKEN", "clerk-session-token-abc")
    FakeAsyncClient.captured_headers = []

    async def scenario():
        db = FakeDB()
        flow = create_flow_plan(
            "bty",
            "admin schedule",
            [{"type": "GET", "url": "/api/admin/schedule"}],
            "op",
            db,
            auth={"type": "env_bearer", "env": "ERRAGENT_BTY_ADMIN_TOKEN"},
        )
        assert flow["auth"] == {"type": "env_bearer", "env": "ERRAGENT_BTY_ADMIN_TOKEN"}
        proposal = create_flow_proposal(flow["_id"], "op", db)
        assert "auth: env_bearer" in proposal["summary"]
        completed = await execute_flow(db, proposal["_id"], "approver")
        assert completed["status"] == "succeeded"
        assert FakeAsyncClient.captured_headers[0]["Authorization"] == "Bearer clerk-session-token-abc"

    asyncio.run(scenario())


def test_flow_env_bearer_missing_token_fails(monkeypatch):
    _patch_client(monkeypatch, [FakeResponse(200)])
    monkeypatch.delenv("ERRAGENT_BTY_ADMIN_TOKEN", raising=False)

    async def scenario():
        db = FakeDB()
        flow = create_flow_plan(
            "bty",
            "admin schedule",
            [{"type": "GET", "url": "/api/admin/schedule"}],
            "op",
            db,
            auth={"type": "env_bearer", "env": "ERRAGENT_BTY_ADMIN_TOKEN"},
        )
        proposal = create_flow_proposal(flow["_id"], "op", db)
        completed = await execute_flow(db, proposal["_id"], "approver")
        assert completed["status"] == "failed"
        assert "ERRAGENT_BTY_ADMIN_TOKEN" in completed["result"]["failure"]

    asyncio.run(scenario())


def test_flow_auth_validation():
    db = FakeDB()
    with pytest.raises(PatchyFlowError, match="UPPER_SNAKE"):
        create_flow_plan("bty", "x", [{"type": "GET", "url": "/a"}], "op", db, auth={"type": "env_bearer", "env": "lowercase"})
    with pytest.raises(PatchyFlowError, match="ERRAGENT_"):
        create_flow_plan("bty", "x", [{"type": "GET", "url": "/a"}], "op", db, auth={"type": "env_bearer", "env": "SECRET_KEY"})
    with pytest.raises(PatchyFlowError, match="not enabled yet"):
        create_flow_plan("bty", "x", [{"type": "GET", "url": "/a"}], "op", db, auth={"type": "clerk_session_token"})


def test_fuzz_flow_requires_staging(monkeypatch):
    db = FakeDB()
    action = [{
        "type": "fuzz",
        "url": "/api/contact",
        "field": "email",
        "body": {"email": "{{fuzz_value}}"},
        "catalog": "email",
    }]
    monkeypatch.delenv("ERRAGENT_BTY_SYNTHETIC_ENV", raising=False)
    with pytest.raises(PatchyFlowError, match="staging-only"):
        create_flow_plan("bty", "email validation", action, "op", db)

    monkeypatch.setenv("ERRAGENT_BTY_SYNTHETIC_ENV", "staging")
    flow = create_flow_plan("bty", "email validation", action, "op", db)
    assert flow["has_fuzz"] is True
    proposal = create_validation_proposal("bty", "op", db)
    assert proposal["kind"] == "validation_audit"
    assert proposal["action"]["flowCount"] == 1

    monkeypatch.delenv("ERRAGENT_BTY_SYNTHETIC_ENV", raising=False)
    monkeypatch.setenv("ERRAGENT_BTY_SYNTHETIC_MUTATIONS_SAFE", "true")
    production_safe_flow = create_flow_plan("bty", "production-safe email validation", action, "op", db)
    assert production_safe_flow["synthetic_mode"] == "production_safe_mutation"


def test_fuzz_flow_stops_on_unexpected_success(monkeypatch):
    monkeypatch.setenv("ERRAGENT_BTY_SYNTHETIC_ENV", "staging")
    _patch_client(monkeypatch, [
        FakeResponse(422, json_data={"detail": "invalid"}),
        FakeResponse(200, json_data={"accepted": True}),
        FakeResponse(422, json_data={"detail": "invalid"}),
    ])

    async def scenario():
        db = FakeDB()
        flow = create_flow_plan(
            "bty",
            "email validation",
            [{
                "type": "fuzz",
                "url": "/api/contact",
                "field": "email",
                "body": {"email": "{{fuzz_value}}"},
                "inputs": ["not-an-email", "test@", "@domain.com"],
                "expect_status": 422,
            }],
            "op",
            db,
        )
        proposal = create_flow_proposal(flow["_id"], "op", db)
        completed = await execute_flow(db, proposal["_id"], "approver")
        assert completed["status"] == "failed"
        assert "Unexpected success" in completed["result"]["failure"]
        assert len(completed["result"]["steps"][0]["cases"]) == 2

    asyncio.run(scenario())


def test_compact_flow_syntax():
    actions = _parse_compact_flow_actions([
        "POST", "/api/contact", "BODY", '{"name":"Patchy"}', "ASSERT", "201",
    ])
    assert actions == [
        {"type": "POST", "url": "/api/contact", "body": {"name": "Patchy"}},
        {"type": "assert_status", "equals": 201},
    ]

    with pytest.raises(Exception, match="requires a path"):
        _parse_compact_flow_actions(["GET"])
