import os
import logging
import httpx
from fastapi import HTTPException
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def create_branch_and_commit(
        self, 
        repo: str, 
        base_branch: str, 
        new_branch: str, 
        file_path: str, 
        file_content: str, 
        commit_message: str
    ) -> dict:
        """Creates a new branch and commits file changes using the GitHub Git Data API."""
        async with httpx.AsyncClient() as client:
            base_url = f"{self.api_base}/repos/{repo}"
            
            # 1. Get base branch reference SHA
            ref_res = await client.get(f"{base_url}/git/refs/heads/{base_branch}", headers=self.headers)
            if ref_res.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Could not find base branch '{base_branch}': {ref_res.text}")
            base_sha = ref_res.json()["object"]["sha"]

            # 2. Get base tree SHA
            commit_res = await client.get(f"{base_url}/git/commits/{base_sha}", headers=self.headers)
            base_tree_sha = commit_res.json()["tree"]["sha"]

            # 3. Create blob for the new file content
            blob_res = await client.post(
                f"{base_url}/git/blobs",
                headers=self.headers,
                json={"content": file_content, "encoding": "utf-8"}
            )
            blob_sha = blob_res.json()["sha"]

            # 4. Create new tree
            tree_res = await client.post(
                f"{base_url}/git/trees",
                headers=self.headers,
                json={
                    "base_tree": base_tree_sha,
                    "tree": [{
                        "path": file_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha
                    }]
                }
            )
            new_tree_sha = tree_res.json()["sha"]

            # 5. Create commit
            new_commit_res = await client.post(
                f"{base_url}/git/commits",
                headers=self.headers,
                json={
                    "message": commit_message,
                    "tree": new_tree_sha,
                    "parents": [base_sha]
                }
            )
            new_commit_sha = new_commit_res.json()["sha"]

            # 6. Create branch reference (refs/heads/{new_branch})
            branch_res = await client.post(
                f"{base_url}/git/refs",
                headers=self.headers,
                json={
                    "ref": f"refs/heads/{new_branch}",
                    "sha": new_commit_sha
                }
            )
            
            # If branch already exists (422), allow it to proceed to PR creation
            if branch_res.status_code not in [201, 422]:
                raise HTTPException(status_code=400, detail=f"Failed to create branch: {branch_res.text}")
                
            return {"status_code": branch_res.status_code}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def create_pull_request(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        """Executes actual PR creation via GitHub API."""
        url = f"{self.api_base}/repos/{repo}/pulls"
        payload = {"title": title, "body": body, "head": head, "base": base}
        
        async with httpx.AsyncClient() as client:
            res = await client.post(url, headers=self.headers, json=payload)
            return {"status_code": res.status_code, "data": res.json()}