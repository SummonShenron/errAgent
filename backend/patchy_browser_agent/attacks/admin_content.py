# patchy_browser_agent/attacks/admin_content.py
import json

ALLOWED_KEYS = [
    # These are inferred from your frontend contentSections.
    # Patchy will fuzz outside this list to test backend validation.
    "hero_", "about_", "programs_", "program_card_", "program_feature_",
    "book_", "programs_page_", "consultation_", "about_page_",
    "qualifications_", "testimonials_", "merch_"
]

def _is_allowed_key(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in ALLOWED_KEYS)


async def test_admin_content(client):
    vulnerabilities = []

    # --- 1. Fetch content ----------------------------------------------------
    try:
        res = await client.get("/api/admin/content")
        status = res.status_code
        body = res.json() if status == 200 else res.text

        if status != 200:
            vulnerabilities.append({
                "endpoint": "/api/admin/content",
                "issue": "fetch_failed",
                "response": body,
            })
            return vulnerabilities

        items = body.get("items", {})
        defaults = body.get("defaults", {})

    except Exception as e:
        vulnerabilities.append({
            "endpoint": "/api/admin/content",
            "issue": "exception_fetch",
            "response": str(e),
        })
        return vulnerabilities

    # --- 2. Fuzz content updates --------------------------------------------
    fuzz_cases = [
        # A. Valid update (baseline)
        {"items": items},

        # B. Oversized payload
        {"items": {k: "A" * 50000 for k in items.keys()}},

        # C. HTML/script injection
        {"items": {k: "<script>alert('xss')</script>" for k in items.keys()}},

        # D. Invalid keys (backend should reject)
        {"items": {"invalid_key": "test"}},

        # E. Nested objects (backend should reject)
        {"items": {"hero_title": {"nested": "object"}}},

        # F. Type confusion (numbers instead of strings)
        {"items": {k: 12345 for k in items.keys()}},

        # G. Missing keys (partial update)
        {"items": {k: items[k] for k in list(items.keys())[:3]}},

        # H. Null values
        {"items": {k: None for k in items.keys()}},
    ]

    for case in fuzz_cases:
        try:
            res = await client.put("/api/admin/content", json=case)
            status = res.status_code
            text = res.text.lower()

            issue = None

            # --- Vulnerability detection -------------------------------------
            if status >= 500:
                issue = "server_error"

            elif "traceback" in text or "syntaxerror" in text:
                issue = "leakage"

            elif "<script>" in text or "alert(" in text:
                issue = "unsanitized_input"

            elif status == 200 and "invalid" in text:
                issue = "unexpected_success"

            if issue:
                vulnerabilities.append({
                    "endpoint": "/api/admin/content",
                    "payload": case,
                    "issue": issue,
                    "response": res.text,
                })

        except Exception as e:
            vulnerabilities.append({
                "endpoint": "/api/admin/content",
                "payload": case,
                "issue": "exception",
                "response": str(e),
            })

    return vulnerabilities
