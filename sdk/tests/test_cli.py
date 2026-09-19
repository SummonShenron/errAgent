from pathlib import Path

from click.testing import CliRunner

from erragent.local import cli as cli_module


class _FakeProcess:
    def __init__(self, argv, env=None, cwd=None, creationflags=0):
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.creationflags = creationflags
        self.terminated = False
        self.killed = False
        self.exit_code = None  # None == still running, matching subprocess.Popen.poll()

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        return self.exit_code if self.exit_code is not None else 0

    def terminate(self):
        self.terminated = True
        self.exit_code = -15

    def kill(self):
        self.killed = True
        self.exit_code = -9


def _install_fake_popen(monkeypatch):
    processes = []

    def fake_popen(argv, env=None, cwd=None, creationflags=0):
        process = _FakeProcess(argv, env, cwd, creationflags)
        processes.append(process)
        return process

    monkeypatch.setattr(cli_module.subprocess, "Popen", fake_popen)
    return processes


def _install_healthy_daemon(monkeypatch):
    """The dev command now waits for the daemon's /health endpoint before starting the app —
    tests that don't care about that behavior specifically can use this to make it succeed
    immediately, same as a real daemon that comes up right away."""

    class _FakeHealthResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(cli_module.urllib.request, "urlopen", lambda *a, **k: _FakeHealthResponse())


def test_dev_starts_daemon_then_app_and_injects_local_url(monkeypatch, tmp_path):
    processes = _install_fake_popen(monkeypatch)
    _install_healthy_daemon(monkeypatch)
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
    _install_healthy_daemon(monkeypatch)
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: cmd)

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 0, result.output
    _, app_process = processes
    assert app_process.env["ERRAGENT_LOCAL_URL"] == "http://127.0.0.1:1234"


def test_dev_spawns_daemon_in_new_console_on_windows(monkeypatch, tmp_path):
    processes = _install_fake_popen(monkeypatch)
    _install_healthy_daemon(monkeypatch)
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: cmd)
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    monkeypatch.setattr(cli_module.subprocess, "CREATE_NEW_CONSOLE", 0x00000010, raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 0, result.output
    daemon_process, app_process = processes
    # Only the daemon gets its own window — the app command stays in whatever console
    # `erragent dev` was invoked from, matching the original "one terminal for your app" ask.
    assert daemon_process.creationflags == 0x00000010
    assert app_process.creationflags == 0


def test_dev_does_not_request_new_console_on_non_windows(monkeypatch, tmp_path):
    processes = _install_fake_popen(monkeypatch)
    _install_healthy_daemon(monkeypatch)
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: cmd)
    monkeypatch.setattr(cli_module.sys, "platform", "linux")

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 0, result.output
    daemon_process, _ = processes
    assert daemon_process.creationflags == 0


def test_dev_refuses_to_start_app_when_daemon_crashes_immediately(monkeypatch, tmp_path):
    processes = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: cmd)
    monkeypatch.setattr(cli_module, "_wait_for_daemon_health", lambda process, port, timeout=10.0: (False, 1))

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 1
    assert "daemon exited immediately (code 1)" in result.output
    # The app must never start against a daemon that's already dead.
    assert len(processes) == 1


def test_dev_refuses_to_start_app_when_daemon_never_responds(monkeypatch, tmp_path):
    processes = _install_fake_popen(monkeypatch)
    monkeypatch.setattr(cli_module.shutil, "which", lambda cmd: cmd)
    monkeypatch.setattr(cli_module, "_wait_for_daemon_health", lambda process, port, timeout=10.0: (False, None))

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 1
    assert "did not respond" in result.output
    assert len(processes) == 1
    daemon_process = processes[0]
    # A daemon that's still running but unresponsive gets cleaned up rather than orphaned.
    assert daemon_process.terminated is True


def test_wait_for_daemon_health_returns_true_once_health_endpoint_responds(monkeypatch):
    _install_healthy_daemon(monkeypatch)
    process = _FakeProcess(["python"])
    healthy, exit_code = cli_module._wait_for_daemon_health(process, 9999, timeout=5.0)
    assert healthy is True
    assert exit_code is None


def test_wait_for_daemon_health_detects_immediate_crash_without_waiting_full_timeout(monkeypatch):
    process = _FakeProcess(["python"])
    process.exit_code = 3  # crashed before ever answering /health
    healthy, exit_code = cli_module._wait_for_daemon_health(process, 9999, timeout=5.0)
    assert healthy is False
    assert exit_code == 3


def test_wait_for_daemon_health_times_out_if_daemon_never_answers(monkeypatch):
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: None)

    def _always_unreachable(*args, **kwargs):
        raise cli_module.urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli_module.urllib.request, "urlopen", _always_unreachable)
    process = _FakeProcess(["python"])  # never exits, never answers
    healthy, exit_code = cli_module._wait_for_daemon_health(process, 9999, timeout=0.05)
    assert healthy is False
    assert exit_code is None


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
