import json

from backend.services.patchy_investigation import (
    InvestigationTools,
    build_github_investigation_tools,
    run_investigation_loop,
)


def _tools(act, max_iterations=4, max_retry_nudges=1) -> InvestigationTools:
    return InvestigationTools(
        actions_menu="- read_repo_file — args: path",
        act=act,
        max_iterations=max_iterations,
        max_retry_nudges=max_retry_nudges,
    )


def test_loop_stops_immediately_on_final_with_no_attempts():
    responses = iter([json.dumps({"action": "final"})])
    result = run_investigation_loop(
        question="why did it break?",
        tools=_tools(act=lambda action, args: "unused"),
        generate_text=lambda prompt: next(responses),
    )
    assert result == {"attempts": [], "concluded": True}


def test_loop_executes_a_tool_call_and_feeds_observation_into_next_prompt():
    responses = iter([
        json.dumps({
            "action": "query",
            "purpose": "check the caller",
            "tool_action": "read_repo_file",
            "args": {"path": "utils/helpers.py"},
        }),
        json.dumps({"action": "final"}),
    ])
    prompts_seen = []

    def generate_text(prompt: str) -> str:
        prompts_seen.append(prompt)
        return next(responses)

    result = run_investigation_loop(
        question="why did it break?",
        tools=_tools(act=lambda action, args: f"def helper(): ..."),
        generate_text=generate_text,
    )

    assert result["concluded"] is True
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["purpose"] == "check the caller"
    assert result["attempts"][0]["action_desc"] == "read_repo_file(path=utils/helpers.py)"
    assert result["attempts"][0]["observation"] == "def helper(): ..."
    # The second prompt (asking what to do next) must include the first step's real result —
    # otherwise the model would be reasoning blind about what it already found.
    assert "def helper(): ..." in prompts_seen[1]


def test_retry_nudge_forces_one_more_step_after_a_failed_tool_call():
    responses = iter([
        json.dumps({
            "action": "query",
            "purpose": "read the file",
            "tool_action": "read_repo_file",
            "args": {"path": "missing.py"},
        }),
        # Tries to conclude right after a failure with steps still available — must be rejected.
        json.dumps({"action": "final"}),
        json.dumps({
            "action": "query",
            "purpose": "retry with the right path",
            "tool_action": "read_repo_file",
            "args": {"path": "real.py"},
        }),
        json.dumps({"action": "final"}),
    ])

    def act(tool_action, args):
        if args.get("path") == "missing.py":
            return "ERROR: could not fetch missing.py"
        return "real content"

    result = run_investigation_loop(
        question="why did it break?",
        tools=_tools(act=act, max_iterations=4, max_retry_nudges=1),
        generate_text=lambda prompt: next(responses),
    )

    assert result["concluded"] is True
    assert len(result["attempts"]) == 2
    assert result["attempts"][1]["observation"] == "real content"


def test_forced_final_when_iterations_run_out_without_an_explicit_final():
    # The model never returns action="final" — every step just queries again.
    def generate_text(prompt: str) -> str:
        return json.dumps({
            "action": "query",
            "purpose": "keep looking",
            "tool_action": "read_repo_file",
            "args": {"path": "x.py"},
        })

    result = run_investigation_loop(
        question="why did it break?",
        tools=_tools(act=lambda action, args: "some content", max_iterations=3),
        generate_text=generate_text,
    )

    assert result["concluded"] is False
    assert len(result["attempts"]) == 3


def test_malformed_model_response_is_treated_as_a_failed_step_not_a_crash():
    responses = iter([
        "this is not JSON at all",
        json.dumps({"action": "final"}),
    ])
    result = run_investigation_loop(
        question="why did it break?",
        tools=_tools(act=lambda action, args: "unused"),
        generate_text=lambda prompt: next(responses),
    )
    assert result["concluded"] is True
    assert len(result["attempts"]) == 1
    assert "ERROR" in result["attempts"][0]["observation"]


def test_tool_exception_is_captured_as_an_observation_not_raised():
    def act(tool_action, args):
        raise RuntimeError("boom")

    responses = iter([
        json.dumps({
            "action": "query",
            "purpose": "try it",
            "tool_action": "read_repo_file",
            "args": {"path": "x.py"},
        }),
        json.dumps({"action": "final"}),
    ])
    result = run_investigation_loop(
        question="why did it break?",
        tools=_tools(act=act),
        generate_text=lambda prompt: next(responses),
    )
    assert result["attempts"][0]["observation"] == "ERROR: boom"


class _FakeGitHubService:
    def __init__(self):
        self.fetch_repository_files_calls = []
        self.list_repository_tree_calls = []
        self.fetch_branch_diff_calls = []
        self.list_commits_calls = []

    async def fetch_repository_files(self, repo, branch, paths):
        self.fetch_repository_files_calls.append((repo, branch, paths))
        if paths == ["real.py"]:
            return {"real.py": "print('hi')"}
        return {}

    async def list_repository_tree(self, repo, branch):
        self.list_repository_tree_calls.append((repo, branch))
        return {"branch": branch, "files": ["a.py", "b.py"]}

    async def fetch_branch_diff(self, repo, base, head):
        self.fetch_branch_diff_calls.append((repo, base, head))
        return {"commit_count": 1, "commits": ["fix: thing"], "files_changed": ["a.py"]}

    async def list_commits(self, repo, branch, limit):
        self.list_commits_calls.append((repo, branch, limit))
        return {"status_code": 200, "commits": [{"sha": "abc1234", "message": "fix: thing"}]}


def test_github_investigation_tools_read_repo_file():
    github = _FakeGitHubService()
    tools = build_github_investigation_tools(github, "org/repo", "main")
    observation = tools.act("read_repo_file", {"path": "real.py"})
    assert observation == "print('hi')"
    assert github.fetch_repository_files_calls == [("org/repo", "main", ["real.py"])]


def test_github_investigation_tools_read_repo_file_missing_returns_error():
    github = _FakeGitHubService()
    tools = build_github_investigation_tools(github, "org/repo", "main")
    observation = tools.act("read_repo_file", {"path": "missing.py"})
    assert observation.startswith("ERROR")


def test_github_investigation_tools_list_repo_tree():
    github = _FakeGitHubService()
    tools = build_github_investigation_tools(github, "org/repo", "main")
    observation = tools.act("list_repo_tree", {})
    assert observation == "a.py\nb.py"


def test_github_investigation_tools_diff_branches_requires_both_args():
    github = _FakeGitHubService()
    tools = build_github_investigation_tools(github, "org/repo", "main")
    assert tools.act("diff_branches", {"base": "main"}).startswith("ERROR")
    assert github.fetch_branch_diff_calls == []


def test_github_investigation_tools_diff_branches():
    github = _FakeGitHubService()
    tools = build_github_investigation_tools(github, "org/repo", "main")
    observation = tools.act("diff_branches", {"base": "main", "head": "fix/thing"})
    assert json.loads(observation)["commits"] == ["fix: thing"]


def test_github_investigation_tools_list_commits_defaults_branch_to_seed_branch():
    github = _FakeGitHubService()
    tools = build_github_investigation_tools(github, "org/repo", "main")
    observation = tools.act("list_commits", {})
    assert observation == "abc1234 — fix: thing"
    assert github.list_commits_calls == [("org/repo", "main", 10)]


def test_github_investigation_tools_unrecognized_action():
    github = _FakeGitHubService()
    tools = build_github_investigation_tools(github, "org/repo", "main")
    assert tools.act("delete_everything", {}).startswith("ERROR")
