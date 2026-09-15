from erragent.local.stackwalk import _extract_target_file_candidates, resolve_target_file


def test_extracts_simple_file_reference():
    stack = 'Traceback (most recent call last):\n  File "app.py", line 12, in <module>\n    1/0\nZeroDivisionError'
    candidates = _extract_target_file_candidates(stack, {})
    assert "app.py" in candidates


def test_strips_known_hosting_prefixes():
    stack = 'File "/opt/render/project/src/backend/routes.py", line 5, in handler'
    candidates = _extract_target_file_candidates(stack, {})
    assert "backend/routes.py" in candidates


def test_skips_site_packages_and_venv_frames():
    stack = (
        'File "/usr/lib/python3.11/site-packages/fastapi/routing.py", line 200, in run\n'
        'File "app.py", line 12, in handler'
    )
    candidates = _extract_target_file_candidates(stack, {})
    assert "app.py" in candidates
    assert not any("site-packages" in c for c in candidates)


def test_generates_progressively_shorter_candidates():
    stack = 'File "myapp/backend/services/widgets.py", line 3, in do_thing'
    candidates = _extract_target_file_candidates(stack, {})
    assert "myapp/backend/services/widgets.py" in candidates
    assert "backend/services/widgets.py" in candidates
    assert "services/widgets.py" in candidates


def test_falls_back_to_app_py_when_no_file_line_present():
    assert _extract_target_file_candidates("no file references here", {}) == ["app.py"]


def test_multi_frame_traceback_targets_innermost_frame_not_caller():
    # "most recent call last": the real bug lives in the last frame, not the entrypoint that
    # called into it. Regression test for a live end-to-end finding — the daemon originally
    # targeted the caller (trigger.py) instead of the file with the actual bug (calc.py).
    stack = (
        'Traceback (most recent call last):\n'
        '  File "trigger.py", line 20, in <module>\n'
        '    add_prices([10, 20, 30])\n'
        '  File "calc.py", line 6, in add_prices\n'
        '    total += prices[i]\n'
        'IndexError: list index out of range'
    )
    candidates = _extract_target_file_candidates(stack, {})
    assert candidates[0] == "calc.py"


def test_resolve_target_file_finds_real_file_under_root(tmp_path):
    (tmp_path / "backend").mkdir()
    target = tmp_path / "backend" / "routes.py"
    target.write_text("print('hi')\n", encoding="utf-8")

    stack = 'File "/opt/render/project/src/backend/routes.py", line 5, in handler'
    result = resolve_target_file(tmp_path, stack, {})

    assert result is not None
    relative, absolute = result
    assert relative == "backend/routes.py"
    assert absolute == target.resolve()


def test_resolve_target_file_returns_root_relative_path_for_absolute_frame(tmp_path):
    # Regression test for a live end-to-end finding: a real local traceback frame is an
    # absolute path (e.g. "C:/Users/dev/project/calc.py"), and it can happen to already sit
    # inside `root`. pathlib's "/" operator discards the left operand when joining an absolute
    # right operand, so naively returning the raw candidate string produced an absolute path
    # downstream instead of a clean root-relative one — which broke the sandbox's `git apply`
    # step (it expects relative diff paths, not "C:/Users/...").
    target = tmp_path / "calc.py"
    target.write_text("total = 0\n", encoding="utf-8")

    absolute_frame = str(target).replace("\\", "/")
    stack = f'File "{absolute_frame}", line 1, in add_prices'
    result = resolve_target_file(tmp_path, stack, {})

    assert result is not None
    relative, absolute = result
    assert relative == "calc.py"
    assert ":" not in relative
    assert absolute == target.resolve()


def test_resolve_target_file_returns_none_when_nothing_exists(tmp_path):
    stack = 'File "nowhere/missing.py", line 1, in x'
    assert resolve_target_file(tmp_path, stack, {}) is None


def test_resolve_target_file_refuses_path_traversal(tmp_path):
    # A candidate that tries to climb out of root must never resolve, even if a file
    # happens to exist at that absolute location outside root.
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        stack = 'File "../outside_secret.py", line 1, in x'
        assert resolve_target_file(tmp_path, stack, {}) is None
    finally:
        outside.unlink(missing_ok=True)
