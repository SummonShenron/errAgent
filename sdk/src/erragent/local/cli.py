"""``erragent`` console script: ``erragent serve`` and ``erragent dev``."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import click

_ROOT_OPTION = click.option(
    "--root",
    "root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root the daemon may read from and write to. Never reads or writes outside it.",
)
_PORT_OPTION = click.option("--port", default=8765, show_default=True, help="Port to bind on 127.0.0.1.")
_POLL_INTERVAL_OPTION = click.option(
    "--poll-interval", default=2.0, show_default=True, help="Seconds between status polls while waiting on analysis."
)
_POLL_TIMEOUT_OPTION = click.option(
    "--poll-timeout", default=180.0, show_default=True, help="Give up waiting on analysis after this many seconds."
)
_ENV_FILE_OPTION = click.option(
    "--env-file",
    "env_file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Load ERRAGENT_URL/credentials from this .env file. Defaults to <root>/.env if present. "
    "Variables already set in the environment take priority over the file.",
)


@click.group()
def cli() -> None:
    """erragent-sdk local-dev tools."""


@cli.command()
@_ROOT_OPTION
@_PORT_OPTION
@_POLL_INTERVAL_OPTION
@_POLL_TIMEOUT_OPTION
@_ENV_FILE_OPTION
def serve(root: Path, port: int, poll_interval: float, poll_timeout: float, env_file: Path | None) -> None:
    """Start the local-dev remediation daemon.

    Point your app's ERRAGENT_LOCAL_URL at http://127.0.0.1:<port> (this daemon binds to
    127.0.0.1 only) while it's running. errAgent's logging handler keeps reporting to the
    cloud exactly as before; this daemon additionally reads local source, gets it analyzed,
    and walks you through approving a fix before writing anything to disk.

    The daemon needs its own cloud credentials (ERRAGENT_URL + ERRAGENT_INGEST_SECRET, or
    ERRAGENT_APP_ID/ERRAGENT_APP_SECRET) to forward locally-detected errors for analysis — the
    same values your target app already has in its own .env work here too.
    """
    import uvicorn
    from dotenv import load_dotenv

    from .daemon import create_app

    # The daemon's own logger (erragent.local.daemon) is where every diagnostic and outcome
    # message goes — configure it explicitly here rather than relying on Python's implicit
    # "handler of last resort" (which only surfaces WARNING+ and silently drops INFO), so
    # `erragent serve` is legible standalone without the caller needing to configure logging.
    logging.basicConfig(level=logging.INFO, format="[errAgent] %(message)s")

    resolved_env_file = env_file or (root / ".env")
    if resolved_env_file.is_file():
        load_dotenv(resolved_env_file)  # does not override variables already set in the environment
        click.echo(f"Loaded environment from {resolved_env_file}")

    click.echo(f"erragent local daemon: serving from {root.resolve()} on http://127.0.0.1:{port}")
    click.echo(f"Set ERRAGENT_LOCAL_URL=http://127.0.0.1:{port} before starting your app.")
    app = create_app(root, poll_interval=poll_interval, poll_timeout=poll_timeout)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


@cli.command(
    context_settings={"ignore_unknown_options": True},
    help="Run the local daemon and your app's start command together, in one terminal.\n\n"
    "Example: erragent dev --root . -- uvicorn app:app --reload\n\n"
    "Starts `erragent serve` as its own OS process (so your app's --reload only ever restarts "
    "your app, never the daemon — embedding it in the same process would kill/restart it on "
    "every reload, losing any in-progress analysis or approval prompt), sets ERRAGENT_LOCAL_URL "
    "for the app command automatically, and stops the daemon when the app exits or you Ctrl+C.",
)
@_ROOT_OPTION
@_PORT_OPTION
@_POLL_INTERVAL_OPTION
@_POLL_TIMEOUT_OPTION
@_ENV_FILE_OPTION
@click.argument("app_command", nargs=-1, type=click.UNPROCESSED, required=True)
def dev(
    root: Path,
    port: int,
    poll_interval: float,
    poll_timeout: float,
    env_file: Path | None,
    app_command: tuple[str, ...],
) -> None:
    daemon_argv = [
        sys.executable,
        "-m",
        "erragent.local.cli",
        "serve",
        "--root",
        str(root),
        "--port",
        str(port),
        "--poll-interval",
        str(poll_interval),
        "--poll-timeout",
        str(poll_timeout),
    ]
    if env_file:
        daemon_argv += ["--env-file", str(env_file)]

    click.echo(f"erragent dev: starting daemon on http://127.0.0.1:{port}")
    daemon_process = subprocess.Popen(daemon_argv)

    app_env = os.environ.copy()
    app_env.setdefault("ERRAGENT_LOCAL_URL", f"http://127.0.0.1:{port}")

    click.echo(f"erragent dev: starting app command: {' '.join(app_command)}")
    app_process = subprocess.Popen(list(app_command), env=app_env)

    exit_code = 0
    try:
        exit_code = app_process.wait()
    except KeyboardInterrupt:
        # On most platforms Ctrl+C already reaches both child processes directly (they share
        # the console); this just makes sure the app process is actually gone before we move
        # on to tearing down the daemon below, instead of racing it.
        try:
            exit_code = app_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            app_process.terminate()
            exit_code = app_process.wait()
    finally:
        click.echo("erragent dev: stopping daemon")
        daemon_process.terminate()
        try:
            daemon_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon_process.kill()
            daemon_process.wait()

    sys.exit(exit_code)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
