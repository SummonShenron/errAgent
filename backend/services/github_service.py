# backend/services/github_service.py
import os
import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger("Incident Ops Logger")

class GitHubOpsService:
    def __init__(self):
        self.token = os.getenv("GITHUB_TOKEN")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json"
        }
        self.api_base = "https://api.github.com"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def fetch_branch_diff(self, repo: str, base: str, head: str) -> dict:
        """Fetches commit and file diff context between two branches."""
        url = f"{self.api_base}/repos/{repo}/compare/{base}...{head}"
        
        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=self.headers)
            res.raise_for_status()
            data = res.json()
            
            commits = [c["commit"]["message"].strip() for c in data.get("commits", [])]
            files = [f["filename"] for f in data.get("files", [])]
            
            return {
                "commit_count": len(commits),
                "commits": commits[:10],
                "files_changed": files[:15]
            }

    async def create_pull_request(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        """Executes actual PR creation via GitHub API."""
        url = f"{self.api_base}/repos/{repo}/pulls"
        payload = {"title": title, "body": body, "head": head, "base": base}
        
        async with httpx.AsyncClient() as client:
            res = await client.post(url, headers=self.headers, json=payload)
            return {"status_code": res.status_code, "data": res.json()}