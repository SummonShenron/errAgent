import logging

logger = logging.getLogger("errAgent Logger")

ALLOWED_PREFIXES = [
    "hero_", "about_", "programs_", "program_card_", "program_feature_",
    "book_", "programs_page_", "consultation_", "about_page_",
    "qualifications_", "testimonials_", "merch_"
]

def _is_allowed_key(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in ALLOWED_PREFIXES)


async def test_admin_content(admin_client):
    vulnerabilities = []

    try:
        logger.info("[pentest] Simulating compromised admin attacks on /api/admin/content...")

        # 1. Fetch baseline content state
        res = await admin_client.get("/api/admin/content")
        if res.status_code != 200:
            logger.warning(f"[pentest] Could not fetch content baseline (Status {res.status_code})")
            return vulnerabilities

        body = res.json() if isinstance(res.json(), dict) else {}
        items = body.get("items", {})

        # Select a target key for payload testing
        valid_key = next((k for k in items.keys() if _is_allowed_key(k)), "hero_title")
        original_val = items.get(valid_key, "Default Content")

        # 2. Test Stored XSS / Unsanitized HTML Injection
        xss_payload = "<script>alert('STORED_XSS')</script><img src=x onerror=alert(1)>"
        xss_res = await admin_client.put(
            "/api/admin/content",
            json={"items": {valid_key: xss_payload}}
        )
        if xss_res.status_code in (200, 204):
            verify_xss = await admin_client.get("/api/admin/content")
            if verify_xss.status_code == 200:
                fetched_val = str(verify_xss.json().get("items", {}).get(valid_key, ""))
                if "<script>" in fetched_val or "onerror=" in fetched_val:
                    vulnerabilities.append({
                        "type": "STORED_XSS_PERSISTED",
                        "severity": "HIGH",
                        "endpoint": "/api/admin/content",
                        "field": valid_key,
                        "detail": "Backend persisted unescaped script tags; vulnerable to Stored XSS if rendered raw in frontend."
                    })

        # 3. Test Arbitrary Schema / Non-Whitelisted Key Injection
        illegal_key = "unauthorized_admin_injected_key"
        whitelist_res = await admin_client.put(
            "/api/admin/content",
            json={"items": {illegal_key: "malicious_injection"}}
        )
        if whitelist_res.status_code in (200, 204):
            verify_res = await admin_client.get("/api/admin/content")
            if verify_res.status_code == 200:
                updated_items = verify_res.json().get("items", {})
                if illegal_key in updated_items:
                    vulnerabilities.append({
                        "type": "UNVALIDATED_SCHEMA_EXPANSION",
                        "severity": "MEDIUM",
                        "endpoint": "/api/admin/content",
                        "detail": f"Backend allows persisting non-whitelisted key '{illegal_key}'."
                    })

        # 4. Test Type Confusion / UI Denial of Service
        type_cases = [
            ("nested_object", {valid_key: {"nested": "object_payload"}}),
            ("array_type", {valid_key: ["val1", "val2"]})
        ]
        for label, payload in type_cases:
            tc_res = await admin_client.put("/api/admin/content", json={"items": payload})
            if tc_res.status_code in (200, 204):
                vulnerabilities.append({
                    "type": "TYPE_CONFUSION_ACCEPTED",
                    "severity": "MEDIUM",
                    "endpoint": "/api/admin/content",
                    "detail": f"Endpoint accepted non-string '{label}' for content field, which could break frontend rendering."
                })

        # 5. Restore Original State
        try:
            await admin_client.put("/api/admin/content", json={"items": {valid_key: original_val}})
        except Exception as clean_err:
            logger.warning(f"[pentest] Cleanup failed for key '{valid_key}': {clean_err}")

    except Exception as e:
        logger.error(f"[pentest] Unexpected error during admin_content testing: {e}")

    return vulnerabilities