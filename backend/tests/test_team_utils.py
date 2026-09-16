from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.utils.team_utils import (
    add_team_member,
    create_team,
    get_bootstrap_team_id,
    get_user_team_ids,
    get_user_team_slugs,
    is_global_admin,
    list_teams_for_user,
    user_can_access_team_id,
    validate_team_slug,
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
                # Mongo array-contains semantics: {"groups": "team:acme"} matches any doc
                # whose groups array contains that value.
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


def test_validate_team_slug_rejects_reserved_names():
    with pytest.raises(HTTPException):
        validate_team_slug("Global_Admins")
    with pytest.raises(HTTPException):
        validate_team_slug("incident_managers")


def test_validate_team_slug_rejects_invalid_characters():
    with pytest.raises(HTTPException):
        validate_team_slug("a")  # too short
    with pytest.raises(HTTPException):
        validate_team_slug("Has Spaces")
    assert validate_team_slug("Acme-Corp") == "acme-corp"


def test_create_team_rejects_duplicate_slug():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin@example.com")
    with pytest.raises(HTTPException):
        create_team(db, name="Acme Again", slug="acme", created_by="admin@example.com")


def test_create_team_never_leaks_github_field_shape():
    db = FakeDB()
    team = create_team(db, name="Acme", slug="acme", created_by="admin@example.com")
    assert team["github_configured"] is False
    assert "github_pat_encrypted" not in team
    # github_pat_configured_at IS exposed (None here) so the UI can show "configured 3 days
    # ago" once it's set — only the ciphertext field must never leave the server.
    assert team["github_pat_configured_at"] is None


def test_add_team_member_grants_namespaced_group(monkeypatch):
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin@example.com")
    _seed_user(db, clerk_id="user_1", email="dev@example.com", groups=["Developers"])

    result = add_team_member(db, slug="acme", target_clerk_id=None, target_email="dev@example.com", added_by="admin")

    assert result == {"team": "acme", "member": "dev@example.com", "role": "member"}
    user_doc = db["directory"].find_one({"clerk_id": "user_1"})
    assert "team:acme" in user_doc["groups"]
    assert "Developers" in user_doc["groups"]  # existing groups untouched


def test_add_team_member_is_idempotent():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin@example.com")
    _seed_user(db, clerk_id="user_1", email="dev@example.com", groups=[])

    add_team_member(db, slug="acme", target_clerk_id="user_1", target_email=None, added_by="admin")
    add_team_member(db, slug="acme", target_clerk_id="user_1", target_email=None, added_by="admin")

    user_doc = db["directory"].find_one({"clerk_id": "user_1"})
    assert user_doc["groups"].count("team:acme") == 1


def test_add_team_member_requires_existing_team():
    db = FakeDB()
    with pytest.raises(HTTPException):
        add_team_member(db, slug="nonexistent", target_clerk_id="user_1", target_email=None, added_by="admin")


def test_add_team_member_requires_existing_directory_user():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin@example.com")
    with pytest.raises(HTTPException):
        add_team_member(db, slug="acme", target_clerk_id="nobody", target_email=None, added_by="admin")


def test_get_user_team_slugs_and_is_global_admin():
    current_user = {"groups": ["Developers", "team:acme", "team:beta"]}
    assert set(get_user_team_slugs(current_user)) == {"acme", "beta"}
    assert is_global_admin(current_user) is False
    assert is_global_admin({"groups": ["Global_Admins"]}) is True


def test_list_teams_for_user_scoped_to_membership():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    create_team(db, name="Beta", slug="beta", created_by="admin")

    member_view = list_teams_for_user(db, {"groups": ["team:acme"]})
    assert [t["slug"] for t in member_view] == ["acme"]

    admin_view = list_teams_for_user(db, {"groups": ["Global_Admins"]})
    assert {t["slug"] for t in admin_view} == {"acme", "beta"}

    outsider_view = list_teams_for_user(db, {"groups": ["Developers"]})
    assert outsider_view == []


def test_get_user_team_ids_resolves_slugs_to_object_ids():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    create_team(db, name="Beta", slug="beta", created_by="admin")
    acme_raw_id = db["teams"].find_one({"slug": "acme"})["_id"]

    member = {"groups": ["team:acme"]}
    assert get_user_team_ids(db, member) == [acme_raw_id]

    outsider = {"groups": ["Developers"]}
    assert get_user_team_ids(db, outsider) == []


def test_user_can_access_team_id_bypasses_for_global_admin():
    db = FakeDB()
    create_team(db, name="Acme", slug="acme", created_by="admin")
    create_team(db, name="Beta", slug="beta", created_by="admin")
    acme_id = db["teams"].find_one({"slug": "acme"})["_id"]
    beta_id = db["teams"].find_one({"slug": "beta"})["_id"]

    admin = {"groups": ["Global_Admins"]}
    assert user_can_access_team_id(db, admin, beta_id) is True

    member = {"groups": ["team:acme"]}
    assert user_can_access_team_id(db, member, acme_id) is True
    assert user_can_access_team_id(db, member, beta_id) is False
    assert user_can_access_team_id(db, member, None) is False


def test_get_bootstrap_team_id_resolves_by_slug_and_caches_per_db():
    db = FakeDB()
    create_team(db, name="errAgent Core", slug="core", created_by="admin")
    expected_id = db["teams"].find_one({"slug": "core"})["_id"]

    assert get_bootstrap_team_id(db) == expected_id

    other_db = FakeDB()
    # No "core" team in this fresh DB — cache must not leak the previous db's result across
    # instances (same db-identity invalidation pattern as the service registry cache).
    assert get_bootstrap_team_id(other_db) is None
