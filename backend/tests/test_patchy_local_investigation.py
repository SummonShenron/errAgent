from copy import deepcopy
from datetime import datetime, timezone

from backend.services.patchy_local_investigation import (
    build_local_bridge_investigation_tools,
    create_tool_request,
    get_pending_tool_request,
    submit_tool_result,
)


class FakeCollection:
    def __init__(self):
        self.documents = {}

    def insert_one(self, document):
        self.documents[document["_id"]] = deepcopy(document)

    def find_one(self, query, sort=None):
        matches = [doc for doc in self.documents.values() if self._matches(doc, query)]
        if not matches:
            return None
        if sort:
            field, direction = sort[0]
            matches.sort(key=lambda d: d.get(field) or 0, reverse=direction < 0)
        return deepcopy(matches[0])

    def update_one(self, query, update):
        for doc in self.documents.values():
            if self._matches(doc, query):
                doc.update(update.get("$set", {}))
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    @staticmethod
    def _matches(doc, query):
        for key, value in query.items():
            if doc.get(key) != value:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.collections = {"local_investigation_requests": FakeCollection()}

    def __getitem__(self, name):
        return self.collections[name]


def test_create_tool_request_starts_pending():
    db = FakeDB()
    request_id = create_tool_request(db, "inc-1", "read_local_file", {"path": "app.py"})
    doc = db["local_investigation_requests"].documents[request_id]
    assert doc["status"] == "pending"
    assert doc["incident_id"] == "inc-1"
    assert doc["tool_action"] == "read_local_file"
    assert doc["args"] == {"path": "app.py"}


def test_get_pending_tool_request_returns_oldest_pending_only():
    db = FakeDB()
    db["local_investigation_requests"].insert_one({
        "_id": "req-answered", "incident_id": "inc-1", "tool_action": "read_local_file",
        "args": {}, "status": "answered", "observation": "done", "created_at": 1, "answered_at": 2,
    })
    db["local_investigation_requests"].insert_one({
        "_id": "req-pending", "incident_id": "inc-1", "tool_action": "list_local_tree",
        "args": {}, "status": "pending", "observation": None, "created_at": 2, "answered_at": None,
    })

    request = get_pending_tool_request(db, "inc-1")
    assert request["_id"] == "req-pending"


def test_get_pending_tool_request_returns_none_when_nothing_pending():
    db = FakeDB()
    assert get_pending_tool_request(db, "inc-1") is None


def test_submit_tool_result_marks_answered():
    db = FakeDB()
    request_id = create_tool_request(db, "inc-1", "read_local_file", {"path": "app.py"})
    submit_tool_result(db, "inc-1", request_id, "print('hi')")
    doc = db["local_investigation_requests"].documents[request_id]
    assert doc["status"] == "answered"
    assert doc["observation"] == "print('hi')"


def test_local_bridge_tools_act_returns_observation_once_answered(monkeypatch):
    db = FakeDB()
    tools = build_local_bridge_investigation_tools(db, "inc-1", poll_interval=0.01, poll_timeout=5.0)

    # Simulate the daemon answering shortly after the request is created, by monkeypatching
    # time.sleep to answer the request on its first invocation instead of really sleeping.
    real_sleep = __import__("time").sleep
    calls = {"count": 0}

    def _fake_sleep(seconds):
        calls["count"] += 1
        if calls["count"] == 1:
            pending = get_pending_tool_request(db, "inc-1")
            submit_tool_result(db, "inc-1", pending["_id"], "the real file content")

    monkeypatch.setattr("backend.services.patchy_local_investigation.time.sleep", _fake_sleep)

    observation = tools.act("read_local_file", {"path": "app.py"})
    assert observation == "the real file content"


def test_local_bridge_tools_act_times_out_if_daemon_never_answers(monkeypatch):
    db = FakeDB()
    tools = build_local_bridge_investigation_tools(db, "inc-1", poll_interval=0.01, poll_timeout=0.05)
    observation = tools.act("read_local_file", {"path": "app.py"})
    assert observation.startswith("ERROR")


def test_local_bridge_tools_act_rejects_unrecognized_tool_action():
    db = FakeDB()
    tools = build_local_bridge_investigation_tools(db, "inc-1")
    observation = tools.act("delete_everything", {})
    assert observation.startswith("ERROR")
    # Must never even create a request for an action that isn't in the allowlist.
    assert db["local_investigation_requests"].documents == {}
