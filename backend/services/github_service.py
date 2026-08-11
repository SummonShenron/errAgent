import os
import logging
import base64
import hashlib
import re
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
        commit_message: str,
        expected_base_file_sha256: str | None = None,
        expected_full_file_sha256: str | None = None,
    ) -> dict:
        """Creates a new branch and commits file changes using the GitHub Git Data API."""
        async with httpx.AsyncClient() as client:
            base_url = f"{self.api_base}/repos/{repo}"

            # Safety: GitHub contents API expects full file content, never diff text.
            stripped = (file_content or "").lstrip()
            if stripped.startswith("diff --git ") or stripped.startswith("--- a/") or stripped.startswith("+++ b/"):
                raise HTTPException(
                    status_code=409,
                    detail="Refusing to commit unified diff text as file content.",
                )

            # Extra safety: reject hunk-like payloads and common diff markers anywhere in content.
            if re.search(r"^@@\s", file_content, flags=re.MULTILINE):
                raise HTTPException(
                    status_code=409,
                    detail="Refusing to commit hunk payload (starts with @@) as file content.",
                )
            if re.search(r"^(index\s[0-9a-f]|---\s|\+\+\+\s)", file_content, flags=re.MULTILINE):
                raise HTTPException(
                    status_code=409,
                    detail="Refusing to commit content that contains unified diff marker lines.",
                )

            if isinstance(expected_full_file_sha256, str) and expected_full_file_sha256.strip():
                actual_payload_sha256 = hashlib.sha256(file_content.encode("utf-8")).hexdigest()
                if actual_payload_sha256 != expected_full_file_sha256:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Commit payload hash mismatch. Refusing to commit stale or altered content."
                        ),
                    )

            # Safety: compare against base file size to catch accidental near-total overwrites.
            current_file_size = None
            current_file_hash = None
            contents_res = await client.get(
                f"{base_url}/contents/{file_path}",
                headers=self.headers,
                params={"ref": base_branch},
            )
            if contents_res.status_code == 200:
                payload = contents_res.json()
                encoded_content = payload.get("content")
                if isinstance(encoded_content, str):
                    decoded = base64.b64decode(encoded_content.encode("utf-8"), validate=False)
                    current_file_size = len(decoded)
                    current_file_hash = hashlib.sha256(decoded).hexdigest()

            if isinstance(expected_base_file_sha256, str) and expected_base_file_sha256.strip():
                if current_file_hash and current_file_hash != expected_base_file_sha256:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Base branch file changed since analysis. Re-analyze incident before approving hotfix."
                        ),
                    )

            # Fail closed: full replacement payload should be close to base file size.
            if current_file_size and len(file_content.encode("utf-8")) < int(current_file_size * 0.9):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Refusing suspiciously small replacement content. "
                        "Re-analyze incident to generate a safer patch."
                    ),
                )
            
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

            if branch_res.status_code == 201:
                return {
                    "status_code": branch_res.status_code,
                    "branch_updated": True,
                    "new_commit_sha": new_commit_sha,
                    "branch_name": new_branch,
                    "base_file_hash_verified": bool(current_file_hash),
                }

            # If the branch already exists, repoint it to the newly created commit.
            if branch_res.status_code == 422:
                update_ref_res = await client.patch(
                    f"{base_url}/git/refs/heads/{new_branch}",
                    headers=self.headers,
                    json={
                        "sha": new_commit_sha,
                        "force": True,
                    },
                )
                if update_ref_res.status_code not in [200, 201]:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Failed to update existing branch '{new_branch}' to new commit: "
                            f"{update_ref_res.text}"
                        ),
                    )
                return {
                    "status_code": branch_res.status_code,
                    "branch_updated": True,
                    "new_commit_sha": new_commit_sha,
                    "branch_name": new_branch,
                    "base_file_hash_verified": bool(current_file_hash),
                }

            raise HTTPException(status_code=400, detail=f"Failed to create branch: {branch_res.text}")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def create_pull_request(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        """Executes actual PR creation via GitHub API."""
        url = f"{self.api_base}/repos/{repo}/pulls"
        payload = {"title": title, "body": body, "head": head, "base": base}
        
        async with httpx.AsyncClient() as client:
            res = await client.post(url, headers=self.headers, json=payload)
            return {"status_code": res.status_code, "data": res.json()}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def find_open_pull_request(self, repo: str, head: str) -> dict:
        """Finds an open PR for the given branch head, if one exists."""
        owner, _, branch = repo.partition("/")
        if not owner or not branch:
            return {"status_code": 400, "data": {"message": "Invalid repo format."}}

        url = f"{self.api_base}/repos/{repo}/pulls"
        params = {"state": "open", "head": f"{owner}:{head}"}

        async with httpx.AsyncClient() as client:
            res = await client.get(url, headers=self.headers, params=params)
            data = res.json()
            if res.status_code != 200:
                return {"status_code": res.status_code, "data": data}

            if not data:
                return {"status_code": 404, "data": {"message": "No open PR found for head branch."}}

            pr = data[0]
            return {
                "status_code": 200,
                "data": {
                    "number": pr.get("number"),
                    "html_url": pr.get("html_url"),
                    "head": pr.get("head", {}).get("ref"),
                    "base": pr.get("base", {}).get("ref"),
                },
            }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def merge_pull_request(self, repo: str, pull_number: int, commit_message: str = "Auto-merged hotfix by errAgent") -> dict:
        """Merges an open pull request automatically."""
        url = f"{self.api_base}/repos/{repo}/pulls/{pull_number}/merge"
        payload = {"commit_message": commit_message, "merge_method": "merge"}
        
        async with httpx.AsyncClient() as client:
            res = await client.put(url, headers=self.headers, json=payload)
            return {"status_code": res.status_code, "data": res.json()}