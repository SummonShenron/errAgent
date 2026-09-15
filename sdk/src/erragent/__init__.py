"""errAgent client SDK.

Typical usage::

    import erragent
    erragent.install()
    app.add_middleware(erragent.Middleware)   # optional, FastAPI/Starlette apps only
"""

from __future__ import annotations

import logging

from .client import (
    ErrAgentNotConfigured,
    report_client_error,
    report_client_error_nowait,
    report_incident,
    report_incident_nowait,
)
from .config import load_config, resolve_cloud_credentials
from .context import context, current_context
from .handler import ErrAgentHandler, _install_exception_hooks

__all__ = [
    "install",
    "context",
    "current_context",
    "report_incident",
    "report_incident_nowait",
    "report_client_error",
    "report_client_error_nowait",
    "ErrAgentNotConfigured",
    "ErrAgentHandler",
    "Middleware",
]


def install(logger: logging.Logger | None = None) -> bool:
    """Install errAgent logging handler(s) + exception hooks based on ``ERRAGENT_*`` env vars.

    Installs a cloud handler whenever ``ERRAGENT_URL`` + credentials are configured. When
    ``ERRAGENT_LOCAL_URL`` is also set, the local-dev handler is installed *instead of* the
    cloud one by default — the local daemon already forwards every error it handles to the
    cloud itself, so incident visibility isn't lost, and this avoids reporting the same local
    error twice (once via the daemon, once via a direct cloud report that's guaranteed to fail
    analysis since it can't fetch an uncommitted local fix from GitHub). Set
    ``ERRAGENT_LOCAL_ONLY=false`` to install both handlers anyway (e.g. to keep streaming
    non-error log lines to the shared Live Console during local dev too).

    Returns True if at least one handler was installed.
    """
    config = load_config()
    target_logger = logger or logging.getLogger()
    installed = False

    if config.cloud is not None:
        app_id, secret = resolve_cloud_credentials(config.cloud)
        if not any(
            isinstance(h, ErrAgentHandler) and h.endpoint == f"{config.cloud.url.rstrip('/')}/api/v1/logs"
            for h in target_logger.handlers
        ):
            target_logger.addHandler(
                ErrAgentHandler(
                    erragent_url=config.cloud.url,
                    service=config.cloud.service,
                    ingest_secret=secret,
                    app_id=app_id,
                    timeout_seconds=config.cloud.timeout_seconds,
                )
            )
        installed = True

    if config.local is not None:
        if not any(
            isinstance(h, ErrAgentHandler) and h.endpoint == f"{config.local.url.rstrip('/')}/api/v1/logs"
            for h in target_logger.handlers
        ):
            target_logger.addHandler(
                ErrAgentHandler(
                    erragent_url=config.local.url,
                    service=config.local.service,
                    timeout_seconds=config.local.timeout_seconds,
                )
            )
        installed = True

    if installed:
        if target_logger.level == logging.NOTSET or target_logger.level > logging.INFO:
            target_logger.setLevel(logging.INFO)
        _install_exception_hooks(target_logger)

    return installed


def __getattr__(name: str):
    if name == "Middleware":
        from .middleware import ErrAgentMiddleware

        return ErrAgentMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
