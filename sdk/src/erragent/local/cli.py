"""``erragent`` console script — currently just ``erragent serve``."""

from __future__ import annotations

from pathlib import Path

import click


@click.group()
def cli() -> None:
    """erragent-sdk local-dev tools."""


@cli.command()
@click.option(
    "--root",
    "root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root the daemon may read from and write to. Never reads or writes outside it.",
)
@click.option("--port", default=8765, show_default=True, help="Port to bind on 127.0.0.1.")
@click.option("--poll-interval", default=2.0, show_default=True, help="Seconds between status polls while waiting on analysis.")
@click.option("--poll-timeout", default=180.0, show_default=True, help="Give up waiting on analysis after this many seconds.")
def serve(root: Path, port: int, poll_interval: float, poll_timeout: float) -> None:
    """Start the local-dev remediation daemon.

    Point your app's ERRAGENT_LOCAL_URL at http://127.0.0.1:<port> (this daemon binds to
    127.0.0.1 only) while it's running. errAgent's logging handler keeps reporting to the
    cloud exactly as before; this daemon additionally reads local source, gets it analyzed,
    and walks you through approving a fix before writing anything to disk.
    """
    import uvicorn

    from .daemon import create_app

    click.echo(f"erragent local daemon: serving from {root.resolve()} on http://127.0.0.1:{port}")
    click.echo(f"Set ERRAGENT_LOCAL_URL=http://127.0.0.1:{port} before starting your app.")
    app = create_app(root, poll_interval=poll_interval, poll_timeout=poll_timeout)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
