from erragent.local.local_tools import list_local_tree, read_local_file


def test_read_local_file_returns_content(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    assert read_local_file(tmp_path, "app.py") == "print('hi')\n"


def test_read_local_file_resolves_nested_path(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "routes.py").write_text("x = 1\n", encoding="utf-8")
    assert read_local_file(tmp_path, "backend/routes.py") == "x = 1\n"


def test_read_local_file_empty_path_errors(tmp_path):
    assert read_local_file(tmp_path, "").startswith("ERROR")


def test_read_local_file_returns_error_for_missing_file(tmp_path):
    result = read_local_file(tmp_path, "does_not_exist.py")
    assert result.startswith("ERROR")


def test_read_local_file_refuses_path_traversal(tmp_path):
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        result = read_local_file(tmp_path, "../outside_secret.py")
        assert result.startswith("ERROR")
        assert "secret" not in result
    finally:
        outside.unlink(missing_ok=True)


def test_read_local_file_refuses_absolute_path_outside_root(tmp_path):
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        result = read_local_file(tmp_path, str(outside))
        assert result.startswith("ERROR")
    finally:
        outside.unlink(missing_ok=True)


def test_read_local_file_truncates_large_files(tmp_path):
    (tmp_path / "big.py").write_text("x" * 40000, encoding="utf-8")
    result = read_local_file(tmp_path, "big.py")
    assert "[truncated" in result
    assert len(result) < 40000


def test_list_local_tree_lists_real_files(tmp_path):
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "routes.py").write_text("", encoding="utf-8")

    tree = list_local_tree(tmp_path)
    entries = tree.splitlines()
    assert "app.py" in entries
    assert "backend/routes.py" in entries


def test_list_local_tree_excludes_noise_directories(tmp_path):
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-312.pyc").write_text("", encoding="utf-8")

    tree = list_local_tree(tmp_path)
    entries = tree.splitlines()
    assert entries == ["app.py"]


def test_list_local_tree_caps_entry_count(tmp_path):
    for i in range(450):
        (tmp_path / f"file_{i}.py").write_text("", encoding="utf-8")

    tree = list_local_tree(tmp_path)
    assert len(tree.splitlines()) == 400
