"""Multi-tool investigation loop for the production incident-analysis pipeline.

Ports the Reason -> Act -> Observe -> Decide loop proven in local-rag's Sonic Assistant
(``tool_agent_node``/``run_react_loop`` in local-rag's ``backend/services/agent_workflow.py``)
to errAgent's incident analysis: instead of committing to a fix from a single auto-detected
file's content, the model can decide to read other files, check a branch diff, or look at
recent commits before proposing anything.

Scoped to the production/GitHub-sourced pipeline only (``run_ai_analysis_pipeline`` in
``backend/utils/app_utils.py``). Local-dev incidents (``run_ai_analysis_pipeline_local``) keep
today's single-shot behavior unchanged — that path never constructs ``InvestigationTools``.

Deliberate differences from local-rag's loop:
- No "clarify" action: there's no human on the other end of a machine-ingested incident to ask.
  The loop always resolves to "final" or an honest "nothing more was found" within its
  iteration budget.
- Read-only tools only: no test-execution/CI-dispatch tool, even though
  ``GitHubOpsService.dispatch_test_workflow`` exists — that has a real side effect (a real CI
  run against the user's repo) and a hard precondition (the target repo must already define a
  CI workflow), deliberately left out of this pass.
- The loop itself only decides WHEN to stop investigating (a lightweight "final" signal with no
  patch fields). The actual patch is produced by one dedicated, schema-validated Gemini call
  afterward, in ``_run_analysis_with_source`` — exactly the same ``AIAnalysisSchema``
  structured-output call that ran before this change, just fed a richer prompt. The
  security-critical ``old_snippet``/``new_snippet`` fields always come from that schema-validated
  call, never from the freeform per-step JSON these investigation steps produce.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from backend.prompts.constraints import INVESTIGATION_DECISION_PROMPT

logger = logging.getLogger("errAgent Logger")

_EMPTY_OBSERVATION_VALUES = {"", "[]", "{}", "none", "null", "no results", "no results found"}


def _is_empty_observation(observation: str) -> bool:
    return observation.strip().lower() in _EMPTY_OBSERVATION_VALUES


def _parse_decision_json(raw_text: str) -> dict:
    """Defensive JSON extraction — same approach as local-rag's ``_parse_agent_json``: try a
    direct parse, then a regex-located ``{...}`` block, then give up (an unrecognized decision
    is treated as a failed step, not a crash)."""
    clean_text = (raw_text or "").strip()
    clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"\s*```$", "", clean_text)
    try:
        parsed = json.loads(clean_text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        match = re.search(r"(\{.*\})", clean_text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


def format_attempts(attempts: list[dict]) -> str:
    if not attempts:
        return "(none yet — this is the first step)"
    return "\n\n".join(
        f"Attempt {i} — Purpose: {a['purpose']}\nAction: {a['action_desc']}\nObservation: {a['observation']}"
        for i, a in enumerate(attempts, 1)
    )


@dataclass
class InvestigationTools:
    """Bundles the read-only actions available to the loop with their menu description, so the
    menu text shown to the model and the dispatch logic that executes a chosen action can never
    drift out of sync with each other."""

    actions_menu: str
    act: Callable[[str, dict], str]  # (tool_action, args) -> observation text
    max_iterations: int = 4
    max_retry_nudges: int = 1


def run_investigation_loop(
    *,
    question: str,
    tools: InvestigationTools,
    generate_text: Callable[[str], str],
) -> dict:
    """Runs the Reason -> Act -> Observe -> Decide loop. ``generate_text`` sends one prompt to
    the model and returns its raw text response — kept as a plain callable rather than a
    concrete client reference so tests can substitute a fake without touching the real API.

    Returns ``{"attempts": list[dict], "concluded": bool}``. ``attempts`` is empty if the model
    chose "final" on its very first step (nothing more was needed). ``concluded`` is True if the
    model explicitly chose "final"; False if the loop ran out of iterations and was forced to
    stop. Either way, this loop never produces patch fields itself — see the module docstring.
    """
    attempts: list[dict] = []
    retry_nudge_count = 0
    unretried_inconclusive_tools: set[str] = set()
    concluded = False

    for step in range(tools.max_iterations):
        forced_final = step == tools.max_iterations - 1
        needs_retry_nudge = not forced_final and bool(unretried_inconclusive_tools)

        question_for_step = question
        if forced_final:
            question_for_step += (
                "\n\n(You have used all your investigation steps. Return action=\"final\" now.)"
            )
        if needs_retry_nudge:
            failed_tools = ", ".join(sorted(unretried_inconclusive_tools))
            question_for_step += (
                f"\n\n(One or more of your actions failed or came back empty and was never "
                f"retried ({failed_tools}), and you still have steps remaining — an empty "
                "result often means the wrong path or query, not that nothing exists. Retry it "
                "with corrected information, or choose a different action, before concluding.)"
            )

        prompt = INVESTIGATION_DECISION_PROMPT.format(
            question=question_for_step,
            actions_menu=tools.actions_menu,
            attempts=format_attempts(attempts),
        )

        try:
            raw_text = generate_text(prompt)
            decision = _parse_decision_json(raw_text)
        except Exception:
            logger.exception(
                "[patchy_investigation] step %s failed to produce a usable decision.", step + 1
            )
            break

        action = decision.get("action")
        if action == "final" and needs_retry_nudge and retry_nudge_count < tools.max_retry_nudges:
            # Told to retry and it tried to conclude anyway — force one more real step instead
            # of accepting a premature answer, same protection local-rag's loop uses.
            retry_nudge_count += 1
            continue
        if action == "final":
            concluded = True
            break
        if action != "query":
            attempts.append({
                "purpose": decision.get("purpose", "(unclear)"),
                "action_desc": "(no valid action returned)",
                "observation": "ERROR: model did not return a recognized action",
                "tool_action": "",
                "args": {},
            })
            continue

        tool_action = decision.get("tool_action") or ""
        args = decision.get("args") or {}
        purpose = decision.get("purpose", "Investigating...")
        logger.info("[patchy_investigation] step %s: %s (%s)", step + 1, purpose, tool_action)

        try:
            observation = tools.act(tool_action, args)
        except Exception as exc:
            observation = f"ERROR: {exc}"
        if not isinstance(observation, str):
            observation = json.dumps(observation, default=str)

        if tool_action:
            if tool_action in unretried_inconclusive_tools:
                unretried_inconclusive_tools.discard(tool_action)
            elif observation.startswith("ERROR") or _is_empty_observation(observation):
                unretried_inconclusive_tools.add(tool_action)

        args_summary = ", ".join(f"{k}={v}" for k, v in args.items())
        attempts.append({
            "purpose": purpose,
            "action_desc": f"{tool_action}({args_summary})" if tool_action else "",
            "observation": observation,
            # Kept alongside action_desc's human-readable summary so callers that need to act
            # on a specific step's result (e.g. recovering which path a read_local_file call
            # actually read) don't have to re-parse the summary string.
            "tool_action": tool_action,
            "args": args,
        })

    return {"attempts": attempts, "concluded": concluded}


def build_github_investigation_tools(github: Any, repo: str, branch: str) -> InvestigationTools:
    """Read-only ``GitHubOpsService``-backed tools for the investigation loop. Pass one
    ``GitHubOpsService`` instance reused across the whole call (see
    ``get_github_service_for_team``) so its 45s response cache actually helps when the loop
    reads the same file/tree more than once, and so the team's PAT is only decrypted once."""

    import asyncio

    def _run(coro):
        return asyncio.run(coro)

    def act(tool_action: str, args: dict) -> str:
        if tool_action == "read_repo_file":
            path = args.get("path")
            if not path:
                return "ERROR: no path given"
            files = _run(github.fetch_repository_files(repo, branch, [path]))
            if path not in files:
                return f"ERROR: could not fetch {path}"
            return files[path]
        if tool_action == "list_repo_tree":
            tree = _run(github.list_repository_tree(repo, branch))
            return "\n".join(tree.get("files", []))
        if tool_action == "diff_branches":
            base = args.get("base")
            head = args.get("head")
            if not base or not head:
                return "ERROR: base and head branches are required"
            result = _run(github.fetch_branch_diff(repo, base, head))
            return json.dumps(result)
        if tool_action == "list_commits":
            branch_arg = args.get("branch") or branch
            limit = args.get("limit") or 10
            result = _run(github.list_commits(repo, branch_arg, limit))
            return "\n".join(f"{c['sha']} — {c['message']}" for c in result.get("commits", []))
        return f"ERROR: unrecognized tool_action '{tool_action}'"

    actions_menu = "\n".join([
        "- read_repo_file — args: path (relative file path within the repo)",
        "- list_repo_tree — no args; lists every file path in the repo",
        "- diff_branches — args: base (branch name), head (branch name)",
        "- list_commits — args: branch (branch name, optional, defaults to the incident's "
        "branch), limit (max number of commits, integer, optional)",
    ])

    return InvestigationTools(actions_menu=actions_menu, act=act)
