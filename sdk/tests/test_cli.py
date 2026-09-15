from pathlib import Path

from click.testing import CliRunner

from erragent.local import cli as cli_module


class _FakeProcess:
    def __init__(self, argv, env=None, cwd=None):
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _install_fake_popen(monkeypatch):
    processes = []

    def fake_popen(argv, env=None, cwd=None):
        process = _FakeProcess(argv, env, cwd)
        processes.append(process)
        return process

    monkeypatch.setattr(cli_module.subprocess, "Popen", fake_popen)
    return processes


def test_dev_starts_daemon_then_app_and_injects_local_url(monkeypatch, tmp_path):
    processes = _install_fake_popen(monkeypatch)
    # Deterministic resolution regardless of what's actually on PATH in CI.
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: f"/resolved/{cmd}")

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 0, result.output
    assert len(processes) == 2

    daemon_process, app_process = processes
    assert daemon_process.argv[1:4] == ["-m", "erragent.local.cli", "serve"]
    assert "--port" in daemon_process.argv
    assert daemon_process.argv[daemon_process.argv.index("--port") + 1] == "9999"
    assert daemon_process.cwd == tmp_path

    assert app_process.argv == ["/resolved/python", "-c", "pass"]
    assert app_process.env["ERRAGENT_LOCAL_URL"] == "http://127.0.0.1:9999"
    assert app_process.cwd == tmp_path

    # The daemon must be stopped once the app process exits — it should never outlive it.
    assert daemon_process.terminated is True


def test_dev_does_not_override_an_already_set_local_url(monkeypatch, tmp_path):
    monkeypatch.setenv("ERRAGENT_LOCAL_URL", "http://127.0.0.1:1234")
    processes = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: cmd)

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 0, result.output
    _, app_process = processes
    assert app_process.env["ERRAGENT_LOCAL_URL"] == "http://127.0.0.1:1234"


def test_resolve_app_executable_bare_name_uses_path_search(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: f"/resolved/{cmd}")
    assert cli_module._resolve_app_executable(tmp_path, "uvicorn") == "/resolved/uvicorn"


def test_resolve_app_executable_falls_back_to_original_when_unresolvable(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: None)
    assert cli_module._resolve_app_executable(tmp_path, "uvicorn") == "uvicorn"


def test_resolve_app_executable_relative_path_resolves_against_root(monkeypatch, tmp_path):
    # Mirrors `.venv\Scripts\uvicorn` (no extension) — subprocess.Popen(shell=False) can't
    # resolve this on Windows on its own (confirmed: raises FileNotFoundError), so this must
    # go through shutil.which, joined against --root rather than the process's own cwd.
    seen = {}

    def fake_which(cmd):
        seen["cmd"] = cmd
        return cmd + ".EXE"

    monkeypatch.setattr(cli_module.shutil, "which", fake_which)
    result = cli_module._resolve_app_executable(tmp_path, ".venv/Scripts/uvicorn")

    assert seen["cmd"] == str(tmp_path / ".venv/Scripts/uvicorn")
    assert result == str(tmp_path / ".venv/Scripts/uvicorn") + ".EXE"


def test_resolve_app_executable_absolute_path_used_as_is(monkeypatch, tmp_path):
    absolute = tmp_path / "some" / "python.exe"
    seen = {}

    def fake_which(cmd):
        seen["cmd"] = cmd
        return cmd

    monkeypatch.setattr(cli_module.shutil, "which", fake_which)
    result = cli_module._resolve_app_executable(Path("/somewhere/else"), str(absolute))

    assert seen["cmd"] == str(absolute)
    assert result == str(absolute)
