import logging

logger = logging.getLogger("errAgent Logger")

# Comprehensive Fuzz Payload Matrix
FUZZ_PAYLOADS = [
    # 1. Type Confusion & Null Values
    {"label": "null_value", "payload": {"status": None}},
    {"label": "empty_string", "payload": {"status": ""}},
    {"label": "integer_type", "payload": {"status": 12345}},
    {"label": "boolean_type", "payload": {"status": True}},
    {"label": "array_type", "payload": {"status": ["contacted", "pending"]}},
    {"label": "nested_object", "payload": {"status": {"value": "contacted"}}},

    # 2. Boundary Values & Buffers
    {"label": "large_buffer", "payload": {"status": "A" * 10000}},
    {"label": "null_byte_string", "payload": {"status": "contacted\x00admin"}},

    # 3. Injection & Operator Vectors
    {"label": "xss_injection", "payload": {"status": "<script>alert(1)</script>"}},
    {"label": "sql_injection", "payload": {"status": "' OR '1'='1"}},
    {"label": "nosql_operator_gt", "payload": {"status": {"$gt": ""}}},
    {"label": "nosql_operator_ne", "payload": {"status": {"$ne": None}}},

    # 4. Mass Assignment & Escalation Keys
    {"label": "mass_assignment_admin", "payload": {"status": "contacted", "is_admin": True}},
    {"label": "mass_assignment_role", "payload": {"status": "contacted", "role": "superuser"}},
    {"label": "mass_assignment_user_id", "payload": {"status": "contacted", "user_id": "000000000000000000000000"}}
]

async def test_admin_leads(admin_client, unauth_client=None):
    vulnerabilities = []

    try:
        logger.info("[pentest] Starting combined fuzzing & logic testing for /api/admin/leads...")

        # Step 1: Test BFLA (Unauthenticated Access)
        if unauth_client:
            try:
                unauth_res = await unauth_client.get("/api/admin/leads")
                if unauth_res.status_code == 200:
                    vulnerabilities.append({
                        "type": "BFLA_AUTH_BYPASS",
                        "severity": "CRITICAL",
                        "endpoint": "/api/admin/leads",
                        "detail": "Admin leads endpoint accessible without authentication."
                    })
            except Exception as ex:
                logger.error(f"[pentest] BFLA assertion error: {ex}")

        # Step 2: Fetch target lead record for stateful testing
        res = await admin_client.get("/api/admin/leads")
        if res.status_code != 200:
            logger.warning(f"[pentest] Could not fetch leads for fuzzing (Status {res.status_code})")
            return vulnerabilities

        data = res.json()
        leads = data if isinstance(data, list) else data.get("leads", [])
        if not leads:
            logger.info("[pentest] No lead records available to perform payload testing.")
            return vulnerabilities

        target_lead_id = leads[0].get("_id") or leads[0].get("id")
        target_endpoint = f"/api/admin/leads/{target_lead_id}/status"

        # Step 3: Iterate through Fuzz Matrix
        for item in FUZZ_PAYLOADS:
            label = item["label"]
            payload = item["payload"]

            try:
                fuzz_res = await admin_client.patch(target_endpoint, json=payload)
                status_code = fuzz_res.status_code

                # Assertion 1: Unhandled Exception / Internal Server Error (500)
                if status_code == 500:
                    vulnerabilities.append({
                        "type": "UNHANDLED_EXCEPTION",
                        "severity": "MEDIUM",
                        "endpoint": target_endpoint,
                        "fuzz_test": label,
                        "detail": f"Payload '{label}' triggered unhandled 500 Internal Server Error."
                    })

                # Assertion 2: Mass Assignment Escalation Success
                elif status_code == 200 and "mass_assignment" in label:
                    res_json = fuzz_res.json() if fuzz_res.headers.get("content-type", "").startswith("application/json") else {}
                    if res_json.get("is_admin") is True or res_json.get("role") == "superuser":
                        vulnerabilities.append({
                            "type": "MASS_ASSIGNMENT",
                            "severity": "HIGH",
                            "endpoint": target_endpoint,
                            "fuzz_test": label,
                            "detail": f"Endpoint accepted and bound unauthorized field in payload '{label}'."
                        })

                # Assertion 3: NoSQL Operator Injection Success
                elif status_code == 200 and "nosql_operator" in label:
                    vulnerabilities.append({
                        "type": "NOSQL_INJECTION",
                        "severity": "HIGH",
                        "endpoint": target_endpoint,
                        "fuzz_test": label,
                        "detail": "Endpoint accepted raw NoSQL query operator dict with 200 OK."
                    })

            except Exception as fuzz_err:
                logger.error(f"[pentest] Execution error on fuzz test '{label}': {fuzz_err}")

    except Exception as e:
        logger.error(f"[pentest] Execution error in test_admin_leads: {e}")

    # Guaranteed return of list type
    return vulnerabilities