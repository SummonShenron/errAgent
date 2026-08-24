# patchy_browser_agent/attacks/admin_leads.py

async def test_admin_leads(client):
    print("Testing /api/admin/leads...")

    # 1. Basic fetch
    res = await client.get("/api/admin/leads")
    if res.status_code != 200:
        print("Failed to fetch leads:", res.text)
        return

    leads = res.json()
    print("Fetched leads:", leads)

    # 2. Fuzz lead_id in PATCH
    for lead in leads:
        lead_id = lead["_id"]

        # Try valid update
        await client.patch(f"/api/admin/leads/{lead_id}/status",
                           json={"status": "contacted"})

        # Try invalid ObjectId
        await client.patch("/api/admin/leads/invalid_object_id/status",
                           json={"status": "contacted"})

        # Try NoSQL injection payload
        await client.patch("/api/admin/leads/{$ne:null}/status",
                           json={"status": "contacted"})

        # Try huge payload
        await client.patch(f"/api/admin/leads/{lead_id}/status",
                           json={"status": "A" * 50000})
