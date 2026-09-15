from click.testing import CliRunner

from erragent.local import cli as cli_module


class _FakeProcess:
    def __init__(self, argv, env=None):
        self.argv = list(argv)
        self.env = env
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_dev_starts_daemon_then_app_and_injects_local_url(monkeypatch, tmp_path):
    processes = []

    def fake_popen(argv, env=None):
        process = _FakeProcess(argv, env)
        processes.append(process)
        return process

    monkeypatch.setattr(cli_module.subprocess, "Popen", fake_popen)

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

    assert app_process.argv == ["python", "-c", "pass"]
    assert app_process.env["ERRAGENT_LOCAL_URL"] == "http://127.0.0.1:9999"

    # The daemon must be stopped once the app process exits — it should never outlive it.
    assert daemon_process.terminated is True


def test_dev_does_not_override_an_already_set_local_url(monkeypatch, tmp_path):
    monkeypatch.setenv("ERRAGENT_LOCAL_URL", "http://127.0.0.1:1234")
    processes = []

    def fake_popen(argv, env=None):
        process = _FakeProcess(argv, env)
        processes.append(process)
        return process

    monkeypatch.setattr(cli_module.subprocess, "Popen", fake_popen)

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["dev", "--root", str(tmp_path), "--port", "9999", "--", "python", "-c", "pass"],
    )

    assert result.exit_code == 0, result.output
    _, app_process = processes
    assert app_process.env["ERRAGENT_LOCAL_URL"] == "http://127.0.0.1:1234"
