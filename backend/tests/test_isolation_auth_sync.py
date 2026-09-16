"""get_current_user's directory sync-on-login behavior: an already-provisioned user's
email/full_name should be refreshed from the JWT once Clerk actually supplies real values
(e.g. after enabling the email session-token claim), but never regressed by a placeholder
or a momentarily-missing claim."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone

from starlette.requests import Request

import backend.utils.isolation_auth as isolation_auth


class FakeCollection:
    def __init__(self):
        self.documents = []
        self._next_id = 1

    def insert_one(self, document):
        document = dict(document)
        document.setdefault("_id", self._next_id)
        self._next_id += 1
        self.documents.append(document)
        return type("Result", (), {"inserted_id": document["_id"]})()

    def find_one(self, query):
        for doc in self.documents:
            if self._matches(doc, query):
                return deepcopy(doc)
        return None

    def update_one(self, query, update):
        for doc in self.documents:
            if self._matches(doc, query):
                if "$set" in update:
                    doc.update(update["$set"])
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    @staticmethod
    def _matches(doc, query):
        for key, value in query.items():
            if key == "$or":
                if not any(FakeCollection._matches(doc, clause) for clause in value):
                    return False
            elif doc.get(key) != value:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.collections = {"directory": FakeCollection()}

    def __getitem__(self, name):
        return self.collections[name]


def _make_request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/test",
        "headers": [(b"authorization", b"Bearer faketoken")],
        "query_string": b"", "server": ("test", 80),
    })


def _run(coro):
    return asyncio.run(coro)


def test_new_user_still_auto_provisions_with_placeholder_email(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(isolation_auth, "get_db", lambda: db)
    monkeypatch.setattr(isolation_auth, "decode_access_token", lambda token: {"sub": "user_new"})

    result = _run(isolation_auth.get_current_user(_make_request()))

    assert result["email"] == "user_new@example.com"
    assert result["full_name"] == "errAgent Operator"
    stored = db["directory"].find_one({"clerk_id": "user_new"})
    assert stored["email"] == "user_new@example.com"


def test_existing_user_email_and_name_sync_from_real_jwt_claims(monkeypatch):
    db = FakeDB()
    db["directory"].insert_one({
        "clerk_id": "user_1",
        "email": "user_1@example.com",  # placeholder, from an earlier login before the claim existed
        "full_name": "errAgent Operator",
        "groups": ["Developers"],
        "created_at": datetime.now(timezone.utc),
    })
    monkeypatch.setattr(isolation_auth, "get_db", lambda: db)
    monkeypatch.setattr(
        isolation_auth, "decode_access_token",
        lambda token: {"sub": "user_1", "email": "real.person@company.com", "full_name": "Real Person"},
    )

    result = _run(isolation_auth.get_current_user(_make_request()))

    assert result["email"] == "real.person@company.com"
    assert result["full_name"] == "Real Person"
    stored = db["directory"].find_one({"clerk_id": "user_1"})
    assert stored["email"] == "real.person@company.com"
    assert stored["full_name"] == "Real Person"


def test_missing_claim_never_regresses_already_synced_data(monkeypatch):
    db = FakeDB()
    db["directory"].insert_one({
        "clerk_id": "user_1",
        "email": "real.person@company.com",
        "full_name": "Real Person",
        "groups": ["Developers"],
        "created_at": datetime.now(timezone.utc),
    })
    monkeypatch.setattr(isolation_auth, "get_db", lambda: db)
    # A token missing the email/full_name claims (e.g. a stale cached token) must not stomp
    # the already-good stored profile with a synthesized placeholder or the generic default.
    monkeypatch.setattr(isolation_auth, "decode_access_token", lambda token: {"sub": "user_1"})

    result = _run(isolation_auth.get_current_user(_make_request()))

    assert result["email"] == "real.person@company.com"
    assert result["full_name"] == "Real Person"


def test_no_op_update_when_jwt_matches_stored_data(monkeypatch):
    db = FakeDB()
    db["directory"].insert_one({
        "clerk_id": "user_1",
        "email": "real.person@company.com",
        "full_name": "Real Person",
        "groups": ["Global_Admins"],
        "created_at": datetime.now(timezone.utc),
    })
    monkeypatch.setattr(isolation_auth, "get_db", lambda: db)
    monkeypatch.setattr(
        isolation_auth, "decode_access_token",
        lambda token: {"sub": "user_1", "email": "real.person@company.com", "full_name": "Real Person"},
    )

    result = _run(isolation_auth.get_current_user(_make_request()))

    assert result["groups"] == ["Global_Admins"]
    assert result["email"] == "real.person@company.com"
