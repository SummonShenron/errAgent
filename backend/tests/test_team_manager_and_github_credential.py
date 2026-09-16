"""Team manager tier (promote/demote/remove/list members) and per-team encrypted GitHub
credential storage + factory (multi-tenancy Phase D)."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.services.github_service_factory import get_github_service_for_team
from backend.utils.team_utils import (
    add_team_member,
    clear_team_github_pat,
    create_team,
    get_managed_team_slugs,
    is_team_manager,
    list_team_members,
    remove_team_member,
    set_team_github_pat,
    set_team_member_role,
)


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

    def find(self, query):
        return [deepcopy(doc) for doc in self.documents if self._matches(doc, query)]

    def update_one(self, query, update):
        for doc in self.documents:
            if self._matches(doc, query):
                if "$addToSet" in update:
                    for field, value in update["$addToSet"].items():
                        doc.setdefault(field, [])
                        values = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
                        for item in values:
                            if item not in doc[field]:
                                doc[field].append(item)
                if "$pull" in update:
                    for field, value in update["$pull"].items():
                        if field not in doc:
                            continue
                        if isinstance(value, dict) and "$in" in value:
                            doc[field] = [item for item in doc[field] if item not in value["$in"]]
                        else:
                            doc[field] = [item for item in doc[field] if item != value]
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
            elif isinstance(value, dict) and "$in" in value:
                if doc.get(key) not in value["$in"]:
                    return False
            elif isinstance(doc.get(key), list) and not isinstance(value, dict):
                if value not in doc.get(key):
                    return False
            elif doc.get(key) != value:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.collections = {"teams": FakeCollection(), "directory": FakeCollection()}

    def __getitem__(self, name):
        return self.collections[name]


def _seed_user(db, *, clerk_id, email, groups):
    db["directory"].insert_one(
        {"clerk_id": clerk_id, "email": email, "groups": list(groups), "created_at": datetime.now(timezone.utc)}
    )


# --- create_team auto-grants creator as manager ---

def test_create_team_grants_creator_manager_role():
    db = FakeDB()
    _seed_user(db, clerk_id="creator_1", email="lead@example.com", groups=["Developers"])

    create_team(db, name="Acme", slug="acme", created_by="lead@example.com", creator_clerk_id="creator_1")

    creator = db["directory"].find_one({"clerk_id": "creator_1"})
    assert "team:acme" in creator["groups"]
    assert "team:acme:manager" in creator["groups"]
    assert is_team_manager({"groups": creator["groups"]}, "acme") is True


def test_create_team_without_creator_clerk_id_grants_nothing():
    db = FakeDB()
    team = create_team(db, name="Acme", slug="acme", created_by="admin@example.com")
    assert team["slug"] == "acme"
    # No directory user should have been touched.
    assert db["directory"].documents == []


# --- promote / demote / remove / list members ---

def test_set_team_member_role_promotes_and_demotes():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    _seed_user(db, clerk_id="u1", email="dev@example.com", groups=[])
    add_team_member(db, slug="acme", target_clerk_id="u1", target_email=None, added_by="admin")

    set_team_member_role(db, slug="acme", target_clerk_id="u1", target_email=None, role="manager", updated_by="admin")
    member = db["directory"].find_one({"clerk_id": "u1"})
    assert "team:acme:manager" in member["groups"]
    assert "team:acme" in member["groups"]  # membership retained

    set_team_member_role(db, slug="acme", target_clerk_id="u1", target_email=None, role="member", updated_by="admin")
    member = db["directory"].find_one({"clerk_id": "u1"})
    assert "team:acme:manager" not in member["groups"]
    assert "team:acme" in member["groups"]  # demotion doesn't remove membership


def test_set_team_member_role_rejects_non_member():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    _seed_user(db, clerk_id="u1", email="dev@example.com", groups=[])

    with pytest.raises(HTTPException):
        set_team_member_role(db, slug="acme", target_clerk_id="u1", target_email=None, role="manager", updated_by="admin")


def test_remove_team_member_strips_both_membership_and_manager_tags():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    _seed_user(db, clerk_id="u1", email="dev@example.com", groups=[])
    add_team_member(db, slug="acme", target_clerk_id="u1", target_email=None, added_by="admin", role="manager")

    remove_team_member(db, slug="acme", target_clerk_id="u1", target_email=None, removed_by="admin")

    member = db["directory"].find_one({"clerk_id": "u1"})
    assert "team:acme" not in member["groups"]
    assert "team:acme:manager" not in member["groups"]


def test_list_team_members_reports_roles():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    _seed_user(db, clerk_id="u1", email="manager@example.com", groups=[])
    _seed_user(db, clerk_id="u2", email="member@example.com", groups=[])
    add_team_member(db, slug="acme", target_clerk_id="u1", target_email=None, added_by="admin", role="manager")
    add_team_member(db, slug="acme", target_clerk_id="u2", target_email=None, added_by="admin", role="member")

    roster = {m["email"]: m["role"] for m in list_team_members(db, "acme")}
    assert roster == {"manager@example.com": "manager", "member@example.com": "member"}


def test_get_managed_team_slugs_excludes_plain_membership():
    user = {"groups": ["team:acme", "team:beta", "team:beta:manager"]}
    assert get_managed_team_slugs(user) == ["beta"]


# --- GitHub credential storage ---

def test_set_and_clear_team_github_pat(monkeypatch):
    monkeypatch.setenv("TEAM_SECRET_ENCRYPTION_KEY", "PLACEHOLDER")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("TEAM_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())

    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")

    result = set_team_github_pat(db, slug="acme", pat="ghp_supersecret", set_by="admin")
    assert result["github_configured"] is True
    team = db["teams"].find_one({"slug": "acme"})
    assert team["github_pat_encrypted"] != b"ghp_supersecret"  # actually encrypted, not stored raw
    assert b"ghp_supersecret" not in team["github_pat_encrypted"]

    cleared = clear_team_github_pat(db, slug="acme", cleared_by="admin")
    assert cleared["github_configured"] is False
    team = db["teams"].find_one({"slug": "acme"})
    assert team["github_pat_encrypted"] is None


def test_set_team_github_pat_requires_nonempty_value():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    with pytest.raises(HTTPException):
        set_team_github_pat(db, slug="acme", pat="   ", set_by="admin")


# --- github_service_factory ---

def test_get_github_service_for_team_raises_503_when_unconfigured():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    team = db["teams"].find_one({"slug": "acme"})

    with pytest.raises(HTTPException) as exc_info:
        get_github_service_for_team(db, team["_id"])
    assert exc_info.value.status_code == 503


def test_get_github_service_for_team_returns_client_with_decrypted_token(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("TEAM_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())

    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    set_team_github_pat(db, slug="acme", pat="ghp_realtoken", set_by="admin")
    team = db["teams"].find_one({"slug": "acme"})

    service = get_github_service_for_team(db, team["_id"])
    assert service.token == "ghp_realtoken"
    assert service.headers["Authorization"] == "Bearer ghp_realtoken"


def test_get_github_service_for_team_raises_503_for_unknown_team_id():
    db = FakeDB()
    with pytest.raises(HTTPException) as exc_info:
        get_github_service_for_team(db, "does-not-exist")
    assert exc_info.value.status_code == 503
