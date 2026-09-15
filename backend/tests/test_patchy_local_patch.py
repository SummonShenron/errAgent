import hashlib
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from backend.services.patchy_hitl import PatchyProposalError
from backend.services.patchy_local_patch import (
    ack_local_patch,
    approve_local_patch,
    create_local_patch_proposal,
    get_local_patch_status,
)


class FakeCollection:
    def __init__(self):
        self.documents = {}

    def insert_one(self, document):
        key = document.get("_id", len(self.documents))
        self.documents[key] = deepcopy(document)

    def find_one(self, query, projection=None, sort=None):
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
            if "." in key:
                head, _, tail = key.partition(".")
                if not isinstance(doc.get(head), dict) or doc[head].get(tail) != value:
                    return False
            elif doc.get(key) != value:
                return False
        return True


class FakeDB:
    def __init__(self):
        self.collections = {
            "patchy_proposals": FakeCollection(),
            "incidents": FakeCollection(),
            "remediations": FakeCollection(),
            "audit_logs": FakeCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _make_remediation(db, incident_id: str, content_source="sandbox_applied"):
    content = "print('fixed')\n"
    db["incidents"].insert_one({"_id": incident_id, "status": "fix_proposed"})
    db["remediations"].insert_one({
        "incident_id": incident_id,
        "status": "draft",
        "target_file_path": "app.py",
        "full_file_content": content,
        "full_file_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "base_file_sha256": hashlib.sha256(b"print('broken')\n").hexdigest(),
        "code_patch": "--- a/app.py\n+++ b/app.py\n",
        "content_source": content_source,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })


def test_create_and_poll_local_patch_status():
    db = FakeDB()
    _make_remediation(db, "inc_1")
    remediation = db["remediations"].find_one({"incident_id": "inc_1"})

    proposal = create_local_patch_proposal(db, "inc_1", remediation)
    assert proposal["kind"] == "local_patch"
    assert proposal["status"] == "awaiting_approval"

    status = get_local_patch_status(db, "inc_1")
    assert status["incident_status"] == "fix_proposed"
    assert status["proposal"]["_id"] == proposal["_id"]


def test_approve_local_patch_returns_verified_content_and_claims_once():
    db = FakeDB()
    _make_remediation(db, "inc_2")
    remediation = db["remediations"].find_one({"incident_id": "inc_2"})
    proposal = create_local_patch_proposal(db, "inc_2", remediation)

    result = approve_local_patch(db, proposal["_id"], "developer")
    assert result["target_file_path"] == "app.py"
    assert result["full_file_content"] == "print('fixed')\n"
    assert result["content_source"] == "sandbox_applied"

    claimed = db["patchy_proposals"].find_one({"_id": proposal["_id"]})
    assert claimed["status"] == "running"

    with pytest.raises(PatchyProposalError):
        approve_local_patch(db, proposal["_id"], "developer")


def test_approve_local_patch_rejects_unverified_content_source():
    db = FakeDB()
    _make_remediation(db, "inc_3", content_source="llm_raw")
    remediation = db["remediations"].find_one({"incident_id": "inc_3"})
    proposal = create_local_patch_proposal(db, "inc_3", remediation)

    with pytest.raises(PatchyProposalError):
        approve_local_patch(db, proposal["_id"], "developer")


def test_ack_success_marks_incident_resolved_and_remediation_executed():
    db = FakeDB()
    _make_remediation(db, "inc_4")
    remediation = db["remediations"].find_one({"incident_id": "inc_4"})
    proposal = create_local_patch_proposal(db, "inc_4", remediation)
    approve_local_patch(db, proposal["_id"], "developer")

    result = ack_local_patch(db, proposal["_id"], "developer", "succeeded")
    assert result["status"] == "succeeded"

    incident = db["incidents"].find_one({"_id": "inc_4"})
    assert incident["status"] == "resolved"
    remediation_after = db["remediations"].find_one({"incident_id": "inc_4"})
    assert remediation_after["status"] == "executed"


def test_ack_failure_keeps_incident_in_fix_proposed():
    db = FakeDB()
    _make_remediation(db, "inc_5")
    remediation = db["remediations"].find_one({"incident_id": "inc_5"})
    proposal = create_local_patch_proposal(db, "inc_5", remediation)
    approve_local_patch(db, proposal["_id"], "developer")

    ack_local_patch(db, proposal["_id"], "developer", "failed", detail="base file changed since analysis")

    incident = db["incidents"].find_one({"_id": "inc_5"})
    assert incident["status"] == "fix_proposed"
    remediation_after = db["remediations"].find_one({"incident_id": "inc_5"})
    assert remediation_after["status"] == "local_write_failed"


def test_ack_before_approve_is_rejected():
    db = FakeDB()
    _make_remediation(db, "inc_6")
    remediation = db["remediations"].find_one({"incident_id": "inc_6"})
    proposal = create_local_patch_proposal(db, "inc_6", remediation)

    with pytest.raises(PatchyProposalError):
        ack_local_patch(db, proposal["_id"], "developer", "succeeded")
