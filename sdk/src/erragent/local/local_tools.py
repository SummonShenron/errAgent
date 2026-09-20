"""Local-filesystem tools for the free-text CLI investigation bridge.

Answers tool requests the cloud-side investigation loop makes while locating the file behind a
plain-English description typed into the daemon's console (no stack trace to resolve a path
from) — see ``backend/services/patchy_local_investigation.py`` for the cloud side of this
bridge. Every path is resolved and containment-checked against ``--root`` the same way
``stackwalk.resolve_target_file`` already does for error-triggered incidents; a request can
never read outside the project directory the daemon was started in.
"""

from __future__ import annotations

from pathlib import Path

_MAX_FILE_CHARS = 30000
_MAX_TREE_ENTRIES = 400
_EXCLUDED_PATH_SEGMENTS = ("node_modules", "__pycache__", ".git", "dist", ".venv", "venv")


def read_local_file(root: Path, path: str) -> str:
    if not path:
        return "ERROR: no path given"
    resolved_root = root.resolve()
    candidate = (resolved_root / path).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return "ERROR: path escapes the project root"
    if not candidate.is_file():
        return f"ERROR: {path} is not a file"
    try:
        content = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        return f"ERROR: could not read {path}: {exc}"
    if len(content) > _MAX_FILE_CHARS:
        return content[:_MAX_FILE_CHARS] + f"\n... [truncated — {len(content)} total characters]"
    return content


def list_local_tree(root: Path) -> str:
    resolved_root = root.resolve()
    paths: list[str] = []
    for candidate in sorted(resolved_root.rglob("*")):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(resolved_root).as_posix()
        if any(segment in relative for segment in _EXCLUDED_PATH_SEGMENTS):
            continue
        paths.append(relative)
        if len(paths) >= _MAX_TREE_ENTRIES:
            break
    return "\n".join(paths)
