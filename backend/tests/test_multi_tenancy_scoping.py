"""Multi-tenancy Phase C: team_id stamping at write time and scoped reads.

Covers the pieces that don't already have dedicated coverage elsewhere:
- authenticate_ingest_client resolving team_id (explicit client, legacy shared-secret fallback).
- _store_incident_and_queue_analysis stamping team_id (explicit, and bootstrap fallback).
- list_proposals team-scoped filtering.
- _service_by_alias / create_probe_proposal's optional current_user membership gate.
- app.py's SSE ticket mint/validate round trip.
"""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import BackgroundTasks

from backend.services.patchy_hitl import PatchyProposalError, create_probe_proposal, list_proposals
from backend.utils.app_utils import _store_incident_and_queue_analysis, authenticate_ingest_client


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

    def find_one(self, query, *_args, sort=None, **_kwargs):
        matches = [doc for doc in self.documents if self._matches(doc, query)]
        if not matches:
            return None
        if sort:
            for key, direction in reversed(sort):
                matches.sort(key=lambda d: d.get(key) or 0, reverse=direction < 0)
        return deepcopy(matches[0])

    def find(self, query=None, *_args, **_kwargs):
        query = query or {}

        class Cursor(list):
            def sort(self, key, direction=1):
                return Cursor(sorted(self, key=lambda d: d.get(key) or 0, reverse=direction < 0))

            def limit(self, count):
                return Cursor(self[:count])

        return Cursor(deepcopy(doc) for doc in self.documents if self._matches(doc, query))

    @staticmethod
    def _matches(doc, query):
        for key, value in query.items():
            if isinstance(value, dict) and "$in" in value:
                if doc.get(key) not in value["$in"]:
                    return False
            elif isinstance(value, dict) and "$gte" in value:
                if doc.get(key) is None or doc.get(key) < value["$gte"]:
                    return False
            elif isinstance(value, dict) and "$in" not in value and "$gte" not in value:
                continue
            elif doc.get(key) != value:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.collections = {
            "incidents": FakeCollection(),
            "audit_logs": FakeCollection(),
            "teams": FakeCollection(),
            "ingest_clients": FakeCollection(),
            "patchy_proposals": FakeCollection(),
            "service_registry": FakeCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _seed_bootstrap_team(db, slug="core"):
    db["teams"].insert_one({"_id": "team_bootstrap", "slug": slug})
    return "team_bootstrap"


# --- authenticate_ingest_client team_id resolution ---

def test_authenticate_ingest_client_uses_explicit_client_team_id():
    db = FakeDB()
    _seed_bootstrap_team(db)
    db["ingest_clients"].insert_one({"app_id": "app1", "secret": "s3cr3t", "enabled": True, "team_id": "team_acme"})

    context = authenticate_ingest_client(db, "s3cr3t", "app1")
    assert context["team_id"] == "team_acme"


def test_authenticate_ingest_client_falls_back_to_bootstrap_for_legacy_client():
    db = FakeDB()
    bootstrap_id = _seed_bootstrap_team(db)
    # Client predates Phase C: no team_id field at all.
    db["ingest_clients"].insert_one({"app_id": "app2", "secret": "s3cr3t", "enabled": True})

    context = authenticate_ingest_client(db, "s3cr3t", "app2")
    assert context["team_id"] == bootstrap_id


def test_authenticate_ingest_client_legacy_shared_secret_falls_back_to_bootstrap(monkeypatch):
    import backend.utils.app_utils as app_utils_module

    db = FakeDB()
    bootstrap_id = _seed_bootstrap_team(db)
    monkeypatch.setattr(app_utils_module, "INGEST_WEBHOOK_SECRET", "legacy-secret")

    context = authenticate_ingest_client(db, "legacy-secret", None)
    assert context["team_id"] == bootstrap_id
    assert context["app_id"] is None


# --- _store_incident_and_queue_analysis team_id stamping ---

def test_store_incident_stamps_explicit_team_id():
    db = FakeDB()
    _seed_bootstrap_team(db)
    background_tasks = BackgroundTasks()
    payload = {"service_name": "bty", "error_message": "boom", "stack_trace": "trace"}

    incident_id = _store_incident_and_queue_analysis(db, background_tasks, payload, "actor", team_id="team_acme")

    stored = db["incidents"].find_one({"_id": incident_id})
    assert stored["team_id"] == "team_acme"


def test_store_incident_falls_back_to_bootstrap_team_when_unspecified():
    db = FakeDB()
    bootstrap_id = _seed_bootstrap_team(db)
    background_tasks = BackgroundTasks()
    payload = {"service_name": "bty", "error_message": "boom", "stack_trace": "trace"}

    incident_id = _store_incident_and_queue_analysis(db, background_tasks, payload, "actor")

    stored = db["incidents"].find_one({"_id": incident_id})
    assert stored["team_id"] == bootstrap_id


# --- list_proposals team scoping ---

def test_list_proposals_unscoped_returns_everything():
    db = FakeDB()
    db["patchy_proposals"].insert_one({"_id": "p1", "team_id": "team_a", "created_at": datetime.now(timezone.utc)})
    db["patchy_proposals"].insert_one({"_id": "p2", "team_id": "team_b", "created_at": datetime.now(timezone.utc)})

    assert {p["_id"] for p in list_proposals(db)} == {"p1", "p2"}


def test_list_proposals_scoped_to_team_ids():
    db = FakeDB()
    db["patchy_proposals"].insert_one({"_id": "p1", "team_id": "team_a", "created_at": datetime.now(timezone.utc)})
    db["patchy_proposals"].insert_one({"_id": "p2", "team_id": "team_b", "created_at": datetime.now(timezone.utc)})

    scoped = list_proposals(db, team_ids=["team_a"])
    assert [p["_id"] for p in scoped] == ["p1"]


# --- create_probe_proposal / _service_by_alias membership gate ---

def _seed_service(db, team_id="team_acme"):
    db["service_registry"].insert_one({
        "_id": "svc1",
        "team_id": team_id,
        "service_name": "Acme Service",
        "canonical_key": "acme",
        "short_alias": "acme",
        "url": "https://acme.example.com",
        "health_path": "/health",
    })


def test_create_probe_proposal_stamps_team_id_from_service():
    db = FakeDB()
    _seed_service(db)
    proposal = create_probe_proposal("acme", "operator-1", db)
    assert proposal["team_id"] == "team_acme"


def test_create_probe_proposal_allows_team_member(monkeypatch):
    db = FakeDB()
    _seed_service(db)
    db["teams"].insert_one({"_id": "team_acme", "slug": "acme"})
    member = {"groups": ["team:acme"]}

    proposal = create_probe_proposal("acme", "operator-1", db, current_user=member)
    assert proposal["team_id"] == "team_acme"


def test_create_probe_proposal_rejects_non_member():
    db = FakeDB()
    _seed_service(db)
    db["teams"].insert_one({"_id": "team_acme", "slug": "acme"})
    outsider = {"groups": ["team:other"]}

    with pytest.raises(PatchyProposalError):
        create_probe_proposal("acme", "operator-1", db, current_user=outsider)


def test_create_probe_proposal_allows_global_admin_for_any_team():
    db = FakeDB()
    _seed_service(db)
    db["teams"].insert_one({"_id": "team_acme", "slug": "acme"})
    admin = {"groups": ["Global_Admins"]}

    proposal = create_probe_proposal("acme", "operator-1", db, current_user=admin)
    assert proposal["team_id"] == "team_acme"


# --- SSE ticket mint/validate round trip ---

def test_events_ticket_round_trip(monkeypatch):
    import backend.app.app as app_module

    monkeypatch.setattr(app_module, "EVENTS_TICKET_SECRET", "test-secret")
    db = FakeDB()
    db["teams"].insert_one({"_id": "team_acme", "slug": "acme"})
    member = {"groups": ["team:acme"]}

    ticket = app_module._mint_events_ticket(db, member)
    payload = app_module._validate_events_ticket(ticket)

    assert payload is not None
    assert payload["team_ids"] == ["team_acme"]


def test_events_ticket_rejects_tampered_signature(monkeypatch):
    import backend.app.app as app_module

    monkeypatch.setattr(app_module, "EVENTS_TICKET_SECRET", "test-secret")
    db = FakeDB()
    member = {"groups": []}

    ticket = app_module._mint_events_ticket(db, member)
    tampered = ticket[:-1] + ("0" if ticket[-1] != "0" else "1")

    assert app_module._validate_events_ticket(tampered) is None


def test_events_ticket_rejects_expired(monkeypatch):
    import backend.app.app as app_module

    monkeypatch.setattr(app_module, "EVENTS_TICKET_SECRET", "test-secret")
    monkeypatch.setattr(app_module, "EVENTS_TICKET_TTL_SECONDS", -1)
    db = FakeDB()
    member = {"groups": []}

    ticket = app_module._mint_events_ticket(db, member)
    assert app_module._validate_events_ticket(ticket) is None


def test_events_ticket_global_admin_is_unscoped(monkeypatch):
    import backend.app.app as app_module

    monkeypatch.setattr(app_module, "EVENTS_TICKET_SECRET", "test-secret")
    db = FakeDB()
    admin = {"groups": ["Global_Admins"]}

    ticket = app_module._mint_events_ticket(db, admin)
    payload = app_module._validate_events_ticket(ticket)
    assert payload["team_ids"] is None
