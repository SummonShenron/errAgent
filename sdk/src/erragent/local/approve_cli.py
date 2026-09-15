"""Terminal approval prompt for local_patch proposals.

Preserves the same "nothing runs without an explicit human decision" rule errAgent applies to
every other proposal kind — the only difference here is the human decides in their own
terminal instead of the Clerk console, since only the developer's machine can write the file.
"""

from __future__ import annotations


def prompt_approval(*, target_file_path: str, diff: str, pr_title: str = "") -> bool:
    print("\n" + "=" * 70)
    print(f"[errAgent] Proposed local fix for: {target_file_path}")
    if pr_title:
        print(f"           {pr_title}")
    print("=" * 70)
    print(diff or "(no diff preview available)")
    print("=" * 70)
    try:
        answer = input(f"Apply this fix to {target_file_path}? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in {"y", "yes"}
