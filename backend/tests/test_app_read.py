import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.app.app as app_module
from backend.scripts import manage_app_read_access as manage
from backend.services import render_ops
from backend.utils import app_read_utils as ar

SECRET_TEXT = "my therapist said I should stop seeing him, and my card number is 4111111111111111"
NOW = datetime.now(timezone.utc)


# ---- a small, faithful in-memory database ----------------------------------------------------------------------------

def _get(doc, dotted):
    value = doc
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(doc, query):
    for key, cond in query.items():
        actual = _get(doc, key)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, expected in cond.items():
                if op == "$in" and actual not in expected:
                    return False
                if op == "$ne" and actual == expected:
                    return False
                if op == "$gte" and (actual is None or actual < expected):
                    return False
        elif actual != cond:
            return False
    return True


class Cursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction=-1):
        self.docs.sort(key=lambda d: _get(d, key) or datetime.min.replace(tzinfo=timezone.utc), reverse=direction == -1)
        return self

    def limit(self, count):
        self.docs = self.docs[:count]
        return self

    def __iter__(self):
        return iter(self.docs)


class Col:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def find(self, query=None, projection=None):
        return Cursor(d for d in self.docs if _matches(d, query or {}))

    def find_one(self, query=None, projection=None, sort=None):
        hits = list(self.find(query))
        # Like MongoDB: later keys only break ties in earlier ones, and a missing value sorts lowest. Sorting by the keys in
        # reverse order with a stable sort gives exactly that.
        for key, direction in reversed(sort or []):
            hits.sort(key=lambda d: _get(d, key) or datetime.min.replace(tzinfo=timezone.utc), reverse=direction == -1)
        return hits[0] if hits else None

    def update_one(self, flt, update, upsert=False):
        for doc in self.docs:
            if _matches(doc, flt):
                doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    doc.pop(key, None)
                return


class DB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, Col())


READ_SECRET = "ear_test-secret-one"


def client_doc(app_id="saapp", team="team_a", names=("saapp",), secret=READ_SECRET, **extra):
    doc = {"app_id": app_id, "secret": "ingest-secret", "enabled": True, "team_id": team,
           "read_secret_sha256": ar.hash_read_secret(secret), "read_service_names": list(names)}
    doc.update(extra)
    return doc


def incident(id, service="saapp", team="team_a", environment="production", status="open", hours_ago=2, **extra):
    doc = {"_id": id, "team_id": team, "service_name": service, "environment": environment, "status": status,
           "error_message": f"Boom in {id}\nsecond line", "stack_trace": "Traceback...", "repository": "SummonShenron/SAAPP",
           "fingerprint": f"fp-{id}", "metadata": {}, "created_at": NOW - timedelta(hours=hours_ago),
           "updated_at": NOW - timedelta(hours=hours_ago)}
    doc.update(extra)
    return doc


@pytest.fixture
def world(monkeypatch):
    db = DB()
    db["ingest_clients"].insert_one(client_doc())
    db["ingest_clients"].insert_one(client_doc("bty", team="team_b", names=("bty",), secret="ear_bty-secret"))
    db["incidents"].insert_one(incident("inc_saapp_1"))
    db["incidents"].insert_one(incident("inc_saapp_2", hours_ago=5, status="resolved"))
    db["incidents"].insert_one(incident("inc_bty_1", service="bty", team="team_b"))
    db["incidents"].insert_one(incident("inc_saapp_other_team", team="team_b"))          # right service name, wrong team
    db["incidents"].insert_one(incident("inc_saapp_dev", environment="development"))     # local dev noise
    db["incidents"].insert_one(incident("inc_freetext", metadata={"source": "local_dev_daemon_freetext"}))
    monkeypatch.setattr(app_module, "get_db", lambda: db)
    return db, TestClient(app_module.app)


def H(app_id="saapp", secret=READ_SECRET):
    return {"x-app-id": app_id, "x-read-secret": secret}


# ---- authentication ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {}, {"x-app-id": "saapp"}, {"x-read-secret": READ_SECRET}, H(secret="wrong"), H(app_id="nobody"), H(secret="ingest-secret"),
])
def test_every_bad_credential_gets_the_same_401(world, headers):
    _, client = world
    response = client.get("/api/v1/app/incidents", headers=headers)
    assert response.status_code == 401 and response.json()["detail"] == "Invalid read credentials"


def test_the_ingest_secret_is_not_a_read_credential(world):
    _, client = world
    assert client.get("/api/v1/app/incidents", headers=H(secret="ingest-secret")).status_code == 401


def test_a_disabled_client_cannot_read(world):
    db, client = world
    db["ingest_clients"].update_one({"app_id": "saapp"}, {"$set": {"enabled": False}})
    assert client.get("/api/v1/app/incidents", headers=H()).status_code == 401


def test_a_client_with_no_team_or_no_service_names_fails_closed(world):
    db, client = world
    db["ingest_clients"].update_one({"app_id": "saapp"}, {"$set": {"team_id": None}})
    assert client.get("/api/v1/app/incidents", headers=H()).status_code == 403
    db["ingest_clients"].update_one({"app_id": "saapp"}, {"$set": {"team_id": "team_a", "read_service_names": []}})
    assert client.get("/api/v1/app/incidents", headers=H()).status_code == 403


def test_the_secret_is_only_ever_compared_by_its_hash():
    db = DB()
    db["ingest_clients"].insert_one(client_doc())
    context = ar.authenticate_read_client(db, READ_SECRET, "saapp")
    assert context["team_id"] == "team_a" and context["service_names"] == ["saapp"] and context["actor"] == "APP_READ:saapp"
    assert READ_SECRET not in repr(db["ingest_clients"].docs)


# ---- scoping ----------------------------------------------------------------------------------------------------------

def ids(response):
    return sorted(i["id"] for i in response.json()["incidents"])


def test_an_app_sees_only_its_own_teams_incidents_for_its_own_service_in_production(world):
    _, client = world
    response = client.get("/api/v1/app/incidents", headers=H())
    assert response.status_code == 200
    assert ids(response) == ["inc_saapp_1", "inc_saapp_2"]   # not bty's, not another team's, not dev, not a freetext investigation


def test_two_apps_each_see_only_their_own(world):
    _, client = world
    bty = ids(client.get("/api/v1/app/incidents", headers=H("bty", "ear_bty-secret")))
    # bty's team also owns an incident filed under the name "saapp", but that name is not on bty's credential.
    assert bty == ["inc_bty_1"]
    saapp = ids(client.get("/api/v1/app/incidents", headers=H()))
    assert "inc_bty_1" not in saapp and "inc_saapp_other_team" not in saapp


def test_another_apps_incident_is_a_404_identical_to_one_that_does_not_exist(world):
    _, client = world
    other = client.get("/api/v1/app/incidents/inc_bty_1", headers=H())
    missing = client.get("/api/v1/app/incidents/inc_nope", headers=H())
    wrong_team = client.get("/api/v1/app/incidents/inc_saapp_other_team", headers=H())
    assert other.status_code == missing.status_code == wrong_team.status_code == 404
    assert other.json() == missing.json() == wrong_team.json()


def test_the_status_environment_and_since_filters_narrow_within_the_scope_only(world):
    _, client = world
    assert ids(client.get("/api/v1/app/incidents?status=resolved", headers=H())) == ["inc_saapp_2"]
    assert ids(client.get("/api/v1/app/incidents?since=3h", headers=H())) == ["inc_saapp_1"]
    assert ids(client.get("/api/v1/app/incidents?environment=development", headers=H())) == ["inc_saapp_dev"]
    assert client.get("/api/v1/app/incidents?limit=51", headers=H()).status_code == 422
    assert client.get("/api/v1/app/incidents?status=DROP%20TABLE", headers=H()).status_code == 400
    assert client.get("/api/v1/app/incidents?since=yesterday-ish", headers=H()).status_code == 400


# ---- what comes back --------------------------------------------------------------------------------------------------

def test_conversation_content_in_an_incidents_metadata_never_comes_back(world):
    db, client = world
    db["incidents"].insert_one(incident("inc_leaky", metadata={
        "workflowName": "sonic_assistant", "requestId": "r-1", "node": "memory_save_node",
        "input": {"messages": [SECRET_TEXT]}, "output": {"raw_generation": SECRET_TEXT}, "context": SECRET_TEXT,
        "user_reported_description": SECRET_TEXT, "route": "/api/chat", "statusCode": 500,
    }, error_message="Boom\n" + SECRET_TEXT, stack_trace="Traceback\n" + "x" * 9000))
    listing = client.get("/api/v1/app/incidents", headers=H())
    detail = client.get("/api/v1/app/incidents/inc_leaky", headers=H())
    assert detail.status_code == 200
    meta = detail.json()["incident"]["metadata"]
    assert meta == {"workflowName": "sonic_assistant", "requestId": "r-1", "node": "memory_save_node", "route": "/api/chat", "statusCode": 500}
    assert "therapist" not in detail.text.split("Boom")[0]
    assert "4111111111111111" not in listing.text and "4111111111111111" not in detail.text.replace("x" * 4000, "")
    assert len(detail.json()["incident"]["stack_trace"]) <= ar.MAX_STACK_CHARS + 1


def test_the_list_view_is_small_and_has_no_stack_trace_or_metadata(world):
    _, client = world
    row = client.get("/api/v1/app/incidents", headers=H()).json()["incidents"][0]
    assert set(row) == {"id", "service", "environment", "status", "message", "created_at", "updated_at", "severity", "root_cause",
                        "fix_status", "pr_url"}
    assert row["message"] == "Boom in inc_saapp_1"      # only the first line


def test_the_latest_analysis_and_fix_status_are_joined_in(world):
    db, client = world
    db["analyses"].insert_one({"incident_id": "inc_saapp_1", "root_cause_summary": "old", "severity": "low", "created_at": NOW - timedelta(hours=3)})
    db["analyses"].insert_one({"incident_id": "inc_saapp_1", "root_cause_summary": "Missing await on a coroutine", "severity": "high",
                               "suggested_fix": "Add await.", "created_at": NOW - timedelta(hours=1)})
    db["remediations"].insert_one({"incident_id": "inc_saapp_1", "status": "pr_opened", "pr_url": "https://github.com/o/r/pull/9",
                                   "created_at": NOW})
    row = [i for i in client.get("/api/v1/app/incidents", headers=H()).json()["incidents"] if i["id"] == "inc_saapp_1"][0]
    assert row["severity"] == "high" and row["root_cause"] == "Missing await on a coroutine"
    assert row["fix_status"] == "pr_opened" and row["pr_url"].endswith("/pull/9")
    detail = client.get("/api/v1/app/incidents/inc_saapp_1", headers=H()).json()["incident"]
    assert detail["suggested_fix"] == "Add await." and detail["fingerprint"] == "fp-inc_saapp_1"


def test_metadata_values_are_short_scalars_and_nesting_is_dropped():
    clean = ar.sanitize_metadata({"route": "x" * 500, "line": 12, "local_dev": True, "node": {"deep": "text"}, "path": ["a"], "unknown": "y"})
    assert clean["line"] == 12 and clean["local_dev"] is True and len(clean["route"]) <= ar.MAX_META_VALUE_CHARS + 1
    assert set(clean) == {"route", "line", "local_dev"}
    assert ar.sanitize_metadata(None) == {} and ar.sanitize_metadata("text") == {}


# ---- the look-back window ---------------------------------------------------------------------------------------------

def test_since_accepts_relative_and_iso_and_never_reaches_further_back_than_thirty_days():
    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    assert ar.parse_since("24h", now) == now - timedelta(hours=24)
    assert ar.parse_since("30m", now) == now - timedelta(minutes=30)
    assert ar.parse_since("7d", now) == now - timedelta(days=7)
    assert ar.parse_since(None, now) == now - timedelta(days=7)
    assert ar.parse_since("9999d", now) == now - timedelta(days=30)
    assert ar.parse_since("2020-01-01T00:00:00Z", now) == now - timedelta(days=30)
    assert ar.parse_since("2026-10-09T00:00:00", now) == datetime(2026, 10, 9, tzinfo=timezone.utc)
    with pytest.raises(HTTPException) as excinfo:
        ar.parse_since("soon", now)
    assert excinfo.value.status_code == 400


# ---- deploy status -----------------------------------------------------------------------------------------------------

def test_deploy_status_uses_the_apps_own_render_service_and_nothing_from_the_request(world, monkeypatch):
    db, client = world
    asked = []

    async def fake(service_id):
        asked.append(service_id)
        return {"provider": "render", "status": "ok"}

    monkeypatch.setattr(app_module, "collect_render_service_status", fake)
    db["ingest_clients"].update_one({"app_id": "saapp"}, {"$set": {"render_service_id": "srv-abc123def"}})
    assert client.get("/api/v1/app/deploy?service_id=srv-someone-else", headers=H()).json()["status"] == "ok"
    assert asked == ["srv-abc123def"]
    assert client.get("/api/v1/app/deploy", headers=H("nobody")).status_code == 401


def run(coro):
    return asyncio.run(coro)


def test_render_status_is_not_configured_without_a_valid_service_id_or_api_key(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "key")
    for bad in (None, "", "srv-", "../../services", "srv-abc/../x", "srv-abc def"):
        assert run(render_ops.collect_render_service_status(bad))["status"] == "not_configured"
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    result = run(render_ops.collect_render_service_status("srv-abc123def"))
    assert result["status"] == "not_configured" and "RENDER_API_KEY" in result["reason"]


def test_render_status_reports_the_latest_deploy_and_never_the_key_or_a_raw_error(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "super-secret-render-key")

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fetch(client, service_id):
        return {"service": {"name": "saapp", "type": "web_service", "suspended": "not_suspended", "updatedAt": "t"},
                "latestDeploy": {"id": "dep-1", "status": "live", "commit": {"id": "abc"}, "createdAt": "c", "finishedAt": "f"}}

    monkeypatch.setattr(render_ops.httpx, "AsyncClient", Client)
    monkeypatch.setattr(render_ops, "_fetch_render_service", fetch)
    result = run(render_ops.collect_render_service_status("srv-abc123def"))
    assert result["status"] == "ok" and result["latestDeploy"] == {"id": "dep-1", "status": "live", "commit": "abc", "createdAt": "c", "finishedAt": "f"}
    assert "super-secret-render-key" not in repr(result)

    async def failing(client, service_id):
        raise render_ops.httpx.ConnectError("could not connect to https://api.render.com with key super-secret-render-key")

    monkeypatch.setattr(render_ops, "_fetch_render_service", failing)
    result = run(render_ops.collect_render_service_status("srv-abc123def"))
    assert result == {"provider": "render", "status": "error", "reason": "The Render API request failed."}


# ---- the credential script -----------------------------------------------------------------------------------------------

def test_enabling_read_access_stores_only_a_hash_and_the_secret_then_authenticates():
    db = DB()
    db["ingest_clients"].insert_one({"app_id": "saapp", "secret": "ingest", "enabled": True, "team_id": "team_a"})
    result = manage.enable_read_access(db, app_id="saapp", service_names=["SAAPP", " saapp ", "Widget"], team_id="team_a",
                                       render_service_id="srv-abc123def")
    stored = db["ingest_clients"].docs[0]
    assert result["read_secret"].startswith("ear_") and result["ingest_secret"] is None
    assert result["read_secret"] not in repr(stored) and stored["read_secret_sha256"] == ar.hash_read_secret(result["read_secret"])
    assert stored["read_service_names"] == ["saapp", "widget"] and stored["render_service_id"] == "srv-abc123def"
    context = ar.authenticate_read_client(db, result["read_secret"], "saapp")
    assert context["service_names"] == ["saapp", "widget"] and context["render_service_id"] == "srv-abc123def"


def test_an_app_with_no_client_record_needs_create_and_then_also_gets_an_ingest_secret():
    db = DB()
    with pytest.raises(ValueError, match="--create"):
        manage.enable_read_access(db, app_id="saapp", service_names=["saapp"], team_id="team_a")
    result = manage.enable_read_access(db, app_id="saapp", service_names=["saapp"], team_id="team_a", create=True)
    assert result["ingest_secret"] and db["ingest_clients"].docs[0]["secret"] == result["ingest_secret"]
    assert db["ingest_clients"].docs[0]["team_id"] == "team_a" and db["ingest_clients"].docs[0]["enabled"] is True


def test_reads_are_never_scoped_across_teams_and_a_service_name_is_required():
    db = DB()
    db["ingest_clients"].insert_one({"app_id": "saapp", "secret": "i", "enabled": True, "team_id": "team_a"})
    with pytest.raises(ValueError, match="different team"):
        manage.enable_read_access(db, app_id="saapp", service_names=["saapp"], team_id="team_b")
    with pytest.raises(ValueError, match="service name"):
        manage.enable_read_access(db, app_id="saapp", service_names=["", "  "], team_id="team_a")


def test_rotating_invalidates_the_old_secret_and_disabling_removes_read_access_but_not_ingest():
    db = DB()
    db["ingest_clients"].insert_one({"app_id": "saapp", "secret": "ingest", "enabled": True, "team_id": "team_a"})
    first = manage.enable_read_access(db, app_id="saapp", service_names=["saapp"], team_id="team_a")["read_secret"]
    second = manage.rotate_read_secret(db, app_id="saapp")
    assert second != first
    with pytest.raises(HTTPException):
        ar.authenticate_read_client(db, first, "saapp")
    assert ar.authenticate_read_client(db, second, "saapp")["app_id"] == "saapp"
    assert manage.disable_read_access(db, app_id="saapp") is True
    with pytest.raises(HTTPException):
        ar.authenticate_read_client(db, second, "saapp")
    assert db["ingest_clients"].docs[0]["secret"] == "ingest" and db["ingest_clients"].docs[0]["enabled"] is True
    assert manage.disable_read_access(db, app_id="saapp") is False
    with pytest.raises(ValueError):
        manage.rotate_read_secret(db, app_id="saapp")


def test_the_services_helper_lists_the_names_incidents_are_actually_filed_under():
    db = DB()
    db["incidents"].insert_one({"_id": "1", "team_id": "t", "service_name": "SAAPP Widget"})
    db["incidents"].insert_one({"_id": "2", "team_id": "t", "service_name": "saapp"})
    db["incidents"].insert_one({"_id": "3", "team_id": "other", "service_name": "bty"})
    assert manage.suggest_service_names(db, "t") == ["saapp", "saapp widget"]


# ---- logs -------------------------------------------------------------------------------------------------------------

from backend.services.log_broker import LogEventInput, log_broker  # noqa: E402


def stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def logs(world):
    asyncio.run(log_broker.clear())

    async def seed():
        rows = [
            ("SAAPP", "info", "request started", 50, {"requestId": "req-1", "node": "reasoner", "erragent_context": {"input": SECRET_TEXT}}),
            ("SAAPP", "warn", "slow provider call", 40, {"requestId": "req-1"}),
            ("saapp", "error", "TypeError: boom", 30, {"requestId": "req-2", "statusCode": 500}),
            ("SAAPP", "info", "very old line", 60 * 5, {}),
            ("BTY", "error", "bty only", 20, {}),
            ("errAgent", "error", "errAgent internal", 10, {}),
        ]
        for service, level, message, minutes, context in rows:
            await log_broker.publish(
                LogEventInput(service=service, level=level, message=message, timestamp=stamp(minutes), context=context), source_app_id=None
            )
        await log_broker.publish(
            LogEventInput(service="SAAPP", level="error", message="from another app under saapp's name", timestamp=stamp(5)),
            source_app_id="someone_else",
        )
        await log_broker.publish(
            LogEventInput(service="SAAPP", level="info", message="from saapp itself", timestamp=stamp(4)), source_app_id="saapp"
        )

    asyncio.run(seed())
    yield world[1]
    asyncio.run(log_broker.clear())


def messages(response):
    return [e["message"] for e in response.json()["entries"]]


def test_an_app_reads_only_its_own_services_log_lines_oldest_first(logs):
    response = logs.get("/api/v1/app/logs", headers=H(), params={"since": "2h"})
    assert response.status_code == 200
    assert messages(response) == ["request started", "slow provider call", "TypeError: boom", "from saapp itself"]


def test_another_apps_lines_under_the_same_service_name_and_other_services_never_appear(logs):
    got = messages(logs.get("/api/v1/app/logs", headers=H(), params={"since": "2h", "limit": 100}))
    assert "from another app under saapp's name" not in got
    assert "bty only" not in got and "errAgent internal" not in got
    assert messages(logs.get("/api/v1/app/logs", headers=H("bty", "ear_bty-secret"), params={"since": "2h"})) == ["bty only"]


def test_the_default_window_is_an_hour_and_it_can_be_widened(logs):
    assert "very old line" not in messages(logs.get("/api/v1/app/logs", headers=H()))
    assert "very old line" in messages(logs.get("/api/v1/app/logs", headers=H(), params={"since": "12h"}))


def test_level_is_a_minimum_severity(logs):
    assert messages(logs.get("/api/v1/app/logs", headers=H(), params={"level": "error", "since": "2h"})) == ["TypeError: boom"]
    assert messages(logs.get("/api/v1/app/logs", headers=H(), params={"level": "warn", "since": "2h"})) == ["slow provider call", "TypeError: boom"]


def test_contains_and_request_id_narrow_the_result(logs):
    assert messages(logs.get("/api/v1/app/logs", headers=H(), params={"contains": "PROVIDER", "since": "2h"})) == ["slow provider call"]
    assert messages(logs.get("/api/v1/app/logs", headers=H(), params={"request_id": "req-1", "since": "2h"})) == ["request started", "slow provider call"]


def test_the_limit_keeps_the_newest_lines_and_the_result_says_how_much_there_was(logs):
    body = logs.get("/api/v1/app/logs", headers=H(), params={"since": "2h", "limit": 2}).json()
    assert [e["message"] for e in body["entries"]] == ["TypeError: boom", "from saapp itself"]
    assert body["matched"] == 4 and body["buffered"] == 5 and body["oldest_buffered"]


def test_only_allowlisted_context_leaves_and_nothing_nested(logs):
    entries = logs.get("/api/v1/app/logs", headers=H(), params={"since": "2h"}).json()["entries"]
    first = next(e for e in entries if e["message"] == "request started")
    assert first["context"] == {"requestId": "req-1", "node": "reasoner"}
    assert "therapist" not in str(entries) and "4111" not in str(entries)


def test_long_messages_are_clipped(world):
    asyncio.run(log_broker.clear())
    asyncio.run(log_broker.publish(LogEventInput(service="saapp", level="error", message="x" * 3500, timestamp=stamp(1))))
    entries = world[1].get("/api/v1/app/logs", headers=H()).json()["entries"]
    assert len(entries[0]["message"]) <= ar.MAX_LOG_MESSAGE_CHARS + 1
    asyncio.run(log_broker.clear())


@pytest.mark.parametrize("params", [{"level": "debug"}, {"request_id": "../etc"}, {"since": "yesterday"}, {"limit": 500}, {"contains": "x" * 81}])
def test_bad_filters_are_refused(logs, params):
    assert logs.get("/api/v1/app/logs", headers=H(), params=params).status_code in (400, 422)


@pytest.mark.parametrize("headers", [{}, H(secret="wrong"), H(app_id="nobody"), H(secret="ingest-secret")])
def test_logs_need_the_read_credential_too(logs, headers):
    response = logs.get("/api/v1/app/logs", headers=headers)
    assert response.status_code == 401 and response.json()["detail"] == "Invalid read credentials"


def test_an_unscoped_client_cannot_read_logs(logs, world):
    world[0]["ingest_clients"].update_one({"app_id": "saapp"}, {"$set": {"read_service_names": []}})
    assert logs.get("/api/v1/app/logs", headers=H()).status_code == 403


def test_list_app_logs_applies_the_service_scope_itself_even_if_handed_other_entries():
    context = {"app_id": "saapp", "team_id": "team_a", "service_names": ["saapp"]}
    entries = [
        {"service": "SAAPP", "level": "info", "message": "mine", "timestamp": stamp(1), "context": {}},
        {"service": "bty", "level": "error", "message": "not mine", "timestamp": stamp(1), "context": {}},
    ]
    result = ar.list_app_logs(entries, context)
    assert [e["message"] for e in result["entries"]] == ["mine"] and result["buffered"] == 1
