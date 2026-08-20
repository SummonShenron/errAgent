import asyncio
from copy import deepcopy
from datetime import timedelta

import backend.services.patchy_hitl as hitl_module
from backend.services.patchy_hitl import PatchyProposalError, approve_and_execute_probe, create_probe_proposal, create_verification_workflow
from backend.services.log_broker import LogBroker


class Result:
    def __init__(self, modified_count=0):
        self.modified_count = modified_count


class FakeCollection:
    def __init__(self):
        self.documents = {}

    def insert_one(self, document):
        self.documents[document["_id"]] = deepcopy(document)

    def find_one(self, query):
        document = self.documents.get(query.get("_id"))
        return deepcopy(document) if document else None

    def update_one(self, query, update):
        document = self.documents.get(query.get("_id"))
        if not document:
            return Result()
        expected_status = query.get("status")
        if expected_status and document.get("status") != expected_status:
            return Result()
        document.update(update.get("$set", {}))
        return Result(modified_count=1)

    def find(self, query, *_args):
        class Cursor(list):
            def limit(self, count):
                return Cursor(self[:count])

        matches = []
        for document in self.documents.values():
            if "service_name" in query:
                alias = query["service_name"]["$regex"].lower()
                if alias not in document.get("service_name", "").lower():
                    continue
            if "status" in query and document.get("status") in query["status"].get("$nin", []):
                continue
            matches.append(deepcopy(document))
        return Cursor(matches)


class FakeDB:
    def __init__(self):
        self.collections = {
            "patchy_proposals": FakeCollection(),
            "incidents": FakeCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


class FakeResponse:
    status_code = 200
    elapsed = timedelta(milliseconds=125)
    text = "ok"

    def json(self):
        return {"status": "ok"}


def test_probe_requires_approval_before_execution(monkeypatch):
    calls = []
    monkeypatch.setattr(hitl_module.requests, "get", lambda url, timeout: calls.append((url, timeout)) or FakeResponse())

    async def scenario():
        db = FakeDB()
        proposal = create_probe_proposal("bty", "operator-1", db)

        assert proposal["status"] == "awaiting_approval"
        assert proposal["risk"] == "read_only"
        assert calls == []

        completed = await approve_and_execute_probe(db, proposal["_id"], "operator-2")
        assert completed["status"] == "succeeded"
        assert completed["approved_by"] == "operator-2"
        assert completed["result"]["httpStatus"] == 200
        assert len(calls) == 1

        try:
            await approve_and_execute_probe(db, proposal["_id"], "operator-2")
        except PatchyProposalError as exc:
            assert "cannot be approved" in str(exc)
        else:
            raise AssertionError("Repeated approval should fail")

    asyncio.run(scenario())


def test_verification_chains_latency_and_returns_report(monkeypatch):
    calls = []
    monkeypatch.setattr(hitl_module.requests, "get", lambda url, timeout: calls.append((url, timeout)) or FakeResponse())

    async def scenario():
        db = FakeDB()
        broker = LogBroker()
        first = create_verification_workflow("bty", "operator-1", db)

        step_one = await approve_and_execute_probe(db, first["_id"], "operator-2", broker=broker)
        assert step_one["workflowStatus"] == "awaiting_approval"
        assert step_one["nextProposal"]["kind"] == "latency_probe"
        assert len(calls) == 1

        step_two = await approve_and_execute_probe(
            db,
            step_one["nextProposal"]["_id"],
            "operator-2",
            broker=broker,
        )
        assert step_two["workflowStatus"] == "completed"
        assert step_two["workflowReport"]["title"] == "BTY Fitness stability: STABLE"
        assert step_two["result"]["medianMs"] == 125
        assert len(calls) == 6

    asyncio.run(scenario())
