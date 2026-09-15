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

    Installs a cloud handler whenever ``ERRAGENT_URL`` + credentials are configured, and
    additionally a local-dev handler whenever ``ERRAGENT_LOCAL_URL`` is set — both fire for
    every log record by default, so local-dev sessions keep streaming into errAgent's Live
    Console while the local daemon also receives the same stream for file-aware remediation.
    Set ``ERRAGENT_LOCAL_ONLY=true`` to skip the cloud handler during local development.

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
