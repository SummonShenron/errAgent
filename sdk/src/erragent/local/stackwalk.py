"""Resolve a stack trace to a real file on the local developer's disk.

Local port of errAgent's ``_extract_target_file_path``/``_extract_target_file_candidates``
(``backend/utils/app_utils.py``) — same "File "..."" regex and candidate-shortening heuristic,
adapted to resolve against a local ``--root`` directory instead of trying a GitHub raw-content
fetch. This is the concrete local-disk replacement for that GitHub fetch step.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _extract_target_file_path(stack_trace: str) -> str:
    """Find the most relevant source file referenced in a traceback.

    Unlike errAgent's GitHub-fetch version of this heuristic (which returns the *first*
    matching frame), this returns the *last* one. A real local Python traceback captured via
    ``logger.exception()`` lists every frame from the outer caller down to where the exception
    was actually raised ("most recent call last") — the first frame is typically just the
    caller (e.g. the script's own entrypoint), while the last is where the bug actually lives.
    Confirmed via a live end-to-end test: picking the first frame targeted the caller instead
    of the file with the real bug.
    """
    if not stack_trace:
        return "app.py"
    best_match = ""
    for line in stack_trace.splitlines():
        if 'File "' in line:
            match = re.search(r'File "([^"]+)"', line)
            if match:
                filepath = match.group(1)
                if "site-packages" not in filepath and "venv" not in filepath and "lib/python" not in filepath:
                    cleaned_path = re.sub(r"^(/opt/render/project/src/|/app/|/var/www/|/workspace/)", "", filepath)
                    best_match = cleaned_path
    return best_match or "app.py"


def _extract_target_file_candidates(stack_trace: str, context: dict[str, Any]) -> list[str]:
    raw_path = _extract_target_file_path(stack_trace).replace("\\", "/").strip()
    if not raw_path:
        return ["app.py"]
    raw_path = raw_path.lstrip("/")

    candidates: list[str] = []
    seen: set[str] = set()

    def add_candidate(value: str) -> None:
        normalized = value.replace("\\", "/").strip().lstrip("/")
        normalized = re.sub(r"/+", "/", normalized)
        if normalized and normalized not in seen:
            seen.add(normalized)
            candidates.append(normalized)

    add_candidate(raw_path)

    for key in ("target_app_location", "app_location", "app_root", "project_root", "source_root", "repo_subdir"):
        hint = context.get(key)
        if not isinstance(hint, str) or not hint.strip():
            continue
        normalized_hint = hint.replace("\\", "/").strip().strip("/")
        if normalized_hint and raw_path.startswith(f"{normalized_hint}/"):
            add_candidate(raw_path[len(normalized_hint) + 1 :])

    parts = [part for part in raw_path.split("/") if part]
    for index in range(1, len(parts) - 1):
        add_candidate("/".join(parts[index:]))

    return candidates or ["app.py"]


def resolve_target_file(root: Path, stack_trace: str, context: dict[str, Any]) -> tuple[str, Path] | None:
    """Return (root-relative path, absolute path) for the first candidate that exists as a real
    file inside ``root``, or None if nothing resolves. Every candidate is containment-checked
    against ``root`` before being considered — a stack trace can never cause a read outside the
    project directory the daemon was started in.

    The returned relative path is always derived from ``candidate_path.relative_to(root)``, not
    the raw candidate string — a candidate can itself be an absolute path (e.g. a real local
    traceback frame like "C:/Users/dev/project/calc.py") that happens to already sit inside
    ``root``, and pathlib's "/" operator discards the left side when joining an absolute right
    side, so the raw candidate is not guaranteed to be root-relative even when it resolves
    inside root. Returning it as-is would send an absolute path downstream, breaking the
    sandbox's ``git apply`` step, which expects a relative diff path.
    """
    root = root.resolve()
    for candidate in _extract_target_file_candidates(stack_trace, context):
        candidate_path = (root / candidate).resolve()
        try:
            relative = candidate_path.relative_to(root)
        except ValueError:
            continue  # escapes root — refuse
        if candidate_path.is_file():
            return relative.as_posix(), candidate_path
    return None
