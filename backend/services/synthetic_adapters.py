import os
from typing import Any


class SyntheticAdapterError(ValueError):
    pass


_ADAPTERS = {
    "sonic": {
        "label": "Sonic Assistant",
        "url_env": "ERRAGENT_SONIC_SYNTHETIC_URL",
        "environment_env": "ERRAGENT_SONIC_SYNTHETIC_ENV",
    },
}


def get_synthetic_adapter(alias: str, allow_production: bool = False) -> dict[str, str]:
    normalized = alias.lower()
    adapter = _ADAPTERS.get(normalized)
    if not adapter:
        raise SyntheticAdapterError("Usage: synthetic ask sonic <question>")
    url = os.getenv(adapter["url_env"], "").strip()
    environment = os.getenv(adapter["environment_env"], "").strip().lower()
    if not url or not environment:
        raise SyntheticAdapterError(
            f"{adapter['label']} synthetic adapter is not configured. Set {adapter['url_env']} "
            f"and {adapter['environment_env']}."
        )
    if environment == "production":
        if not allow_production or os.getenv("ERRAGENT_ALLOW_PRODUCTION_SYNTHETICS", "").lower() not in {"1", "true", "yes"}:
            raise SyntheticAdapterError(
                f"{adapter['label']} production synthetic checks require --production-read-only "
                "and ERRAGENT_ALLOW_PRODUCTION_SYNTHETICS=true."
            )
    elif environment != "staging":
        raise SyntheticAdapterError(f"{adapter['label']} synthetic adapter must target staging or explicitly enabled production.")
    if not url.startswith("https://"):
        raise SyntheticAdapterError("Synthetic adapter URL must use HTTPS.")
    return {"alias": normalized, "label": adapter["label"], "url": url, "environment": environment}


def create_question_proposal(alias: str, question: str, actor: str, db, allow_production: bool = False) -> dict[str, Any]:
    if not isinstance(question, str) or not question.strip():
        raise SyntheticAdapterError("A non-empty question is required.")
    adapter = get_synthetic_adapter(alias, allow_production=allow_production)
    if len(question) > 2000:
        raise SyntheticAdapterError("Question is limited to 2000 characters.")
    from datetime import datetime, timezone
    from uuid import uuid4
    from backend.utils.app_utils import serialize_mongo_doc

    now = datetime.now(timezone.utc)
    proposal = {
        "_id": f"synthetic_question_{uuid4().hex}",
        "kind": "synthetic_question",
        "risk": "production_read_only" if adapter["environment"] == "production" else "staging_only",
        "status": "awaiting_approval",
        "summary": f"Ask {adapter['label']} one question in staging",
        "action": {
            "method": "POST",
            "url": adapter["url"],
            "timeoutSeconds": 30,
            "environment": adapter["environment"],
            "question": question.strip(),
            "assertions": ["HTTP status is 2xx", "response contains a non-empty answer"],
        },
        "adapter": adapter["alias"],
        "created_by": actor,
        "created_at": now,
        "updated_at": now,
    }
    db["patchy_proposals"].insert_one(proposal)
    return serialize_mongo_doc(proposal)
