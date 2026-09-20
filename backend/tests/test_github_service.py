import pytest

from backend.services import github_service as github_service_module
from backend.services.github_service import GitHubOpsService


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Records every GET made through it and answers from a queue of canned responses, in the
    same async-context-manager shape GitHubOpsService's real methods use."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        return self._responses.pop(0)


def _install_fake_client(monkeypatch, responses):
    fake_client = _FakeAsyncClient(responses)
    monkeypatch.setattr(
        github_service_module.httpx, "AsyncClient", lambda *a, **k: fake_client
    )
    return fake_client


@pytest.mark.asyncio
async def test_list_repository_tree_filters_blobs_and_excludes_build_noise(monkeypatch):
    tree_payload = {
        "tree": [
            {"path": "app.py", "type": "blob"},
            {"path": "utils/helpers.py", "type": "blob"},
            {"path": "node_modules/pkg/index.js", "type": "blob"},
            {"path": "src", "type": "tree"},
        ]
    }
    fake_client = _install_fake_client(monkeypatch, [_FakeResponse(200, tree_payload)])

    service = GitHubOpsService(token="test-token")
    result = await service.list_repository_tree("org/repo", "main")

    assert result == {"branch": "main", "files": ["app.py", "utils/helpers.py"]}
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_list_repository_tree_is_cached_across_calls(monkeypatch):
    tree_payload = {"tree": [{"path": "app.py", "type": "blob"}]}
    fake_client = _install_fake_client(monkeypatch, [_FakeResponse(200, tree_payload)])

    service = GitHubOpsService(token="test-token")
    first = await service.list_repository_tree("org/repo", "main")
    second = await service.list_repository_tree("org/repo", "main")

    assert first == second
    # Only one real HTTP call — the second call is served from the 45s response cache.
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_list_repository_tree_rejects_invalid_repo_format(monkeypatch):
    service = GitHubOpsService(token="test-token")
    with pytest.raises(Exception):
        await service.list_repository_tree("not-a-valid-repo", "main")


@pytest.mark.asyncio
async def test_list_commits_returns_short_sha_and_first_message_line(monkeypatch):
    commits_payload = [
        {"sha": "abcdef1234567890", "commit": {"message": "fix: thing\n\nlonger body"}},
        {"sha": "1112223334445556", "commit": {"message": "chore: cleanup"}},
    ]
    _install_fake_client(monkeypatch, [_FakeResponse(200, commits_payload)])

    service = GitHubOpsService(token="test-token")
    result = await service.list_commits("org/repo", "main", limit=10)

    assert result["commits"] == [
        {"sha": "abcdef1", "message": "fix: thing"},
        {"sha": "1112223", "message": "chore: cleanup"},
    ]


@pytest.mark.asyncio
async def test_list_commits_clamps_limit_to_thirty(monkeypatch):
    fake_client = _install_fake_client(monkeypatch, [_FakeResponse(200, [])])

    service = GitHubOpsService(token="test-token")
    await service.list_commits("org/repo", "main", limit=999)

    assert fake_client.calls[0]["params"]["per_page"] == 30


@pytest.mark.asyncio
async def test_list_commits_non_200_returns_empty_list_not_a_crash(monkeypatch):
    _install_fake_client(monkeypatch, [_FakeResponse(404, {"message": "not found"})])

    service = GitHubOpsService(token="test-token")
    result = await service.list_commits("org/repo", "does-not-exist", limit=5)

    assert result == {"status_code": 404, "commits": []}


@pytest.mark.asyncio
async def test_list_repository_tree_requires_token(monkeypatch):
    service = GitHubOpsService(token=None)
    monkeypatch.setattr(github_service_module.os, "getenv", lambda *a, **k: None)
    service.token = None
    with pytest.raises(Exception):
        await service.list_repository_tree("org/repo", "main")
