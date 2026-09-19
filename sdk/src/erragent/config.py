"""Environment-only configuration, matching errAgent's existing convention."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CloudConfig:
    url: str
    service: str
    app_id: str | None
    app_secret: str | None
    ingest_secret: str | None
    timeout_seconds: float


@dataclass(frozen=True)
class LocalConfig:
    url: str
    service: str
    timeout_seconds: float


@dataclass(frozen=True)
class ErrAgentConfig:
    cloud: CloudConfig | None
    local: LocalConfig | None
    local_only: bool


def resolve_cloud_credentials(cloud: CloudConfig) -> tuple[str | None, str | None]:
    """Return (app_id, secret) to actually send, honoring the auth server's "all or nothing"
    per-app credential rule: sending ``x-app-id`` without a matching secret is always rejected
    server-side (`authenticate_ingest_client` never falls back to the legacy secret once an
    app-id header is present), so app_id/app_secret must be used together or not at all — a
    partially-migrated config (app_id set, app_secret not yet set) must fall back to the legacy
    shared secret alone rather than sending a broken per-app credential pair.
    """
    if cloud.app_id and cloud.app_secret:
        return cloud.app_id, cloud.app_secret
    return None, cloud.ingest_secret


def load_config(*, ignore_local_only: bool = False) -> ErrAgentConfig:
    """Resolve SDK configuration from ``ERRAGENT_*`` environment variables.

    Returns a config with ``cloud``/``local`` populated only when their required variables
    are present, so callers can decide which handler(s), if any, to install.

    ``ignore_local_only`` exists for the local daemon (see ``local/daemon.py``): the daemon
    reads the *same* ``.env`` as the app it's serving, which sets ``ERRAGENT_LOCAL_URL`` (and
    thus defaults ``local_only`` to true) to tell the *app* to stop reporting to the cloud
    directly and route through the daemon instead. That signal isn't about the daemon itself —
    the daemon is the thing that's supposed to always talk to the cloud (it's the one doing the
    analysis forwarding) — so without this flag the daemon silently inherits the app's
    local-only setting and refuses to report anything.
    """
    service = os.getenv("ERRAGENT_SERVICE")
    timeout_seconds = float(os.getenv("ERRAGENT_TIMEOUT_SECONDS", "30"))
    local_url = os.getenv("ERRAGENT_LOCAL_URL")

    # Default to local-only whenever local-dev mode is active (ERRAGENT_LOCAL_URL set) — the
    # local daemon already forwards every error it handles to the cloud itself (tagged
    # local_dev=true), so error/incident visibility in the console isn't lost by skipping the
    # app's own direct cloud handler. Without this, every local error produced two incidents:
    # one via the daemon (works, since it has the real local file content) and one via the
    # app's direct cloud handler (guaranteed to fail analysis, since the cloud pipeline can't
    # fetch an uncommitted local-only fix from GitHub) — pure noise, confirmed by live testing.
    # Set ERRAGENT_LOCAL_ONLY=false to opt back into dual-reporting (e.g. to keep streaming
    # non-error log lines to the shared Live Console during local dev too).
    local_only_env = os.getenv("ERRAGENT_LOCAL_ONLY")
    local_only = _truthy(local_only_env) if local_only_env is not None else bool(local_url)

    cloud: CloudConfig | None = None
    cloud_url = os.getenv("ERRAGENT_URL")
    app_id = os.getenv("ERRAGENT_APP_ID")
    app_secret = os.getenv("ERRAGENT_APP_SECRET")
    ingest_secret = os.getenv("ERRAGENT_INGEST_SECRET")
    if cloud_url and service and (ignore_local_only or not local_only) and (ingest_secret or (app_id and app_secret)):
        cloud = CloudConfig(
            url=cloud_url,
            service=service,
            app_id=app_id,
            app_secret=app_secret,
            ingest_secret=ingest_secret,
            timeout_seconds=timeout_seconds,
        )

    local: LocalConfig | None = None
    if local_url and service:
        local = LocalConfig(url=local_url, service=service, timeout_seconds=timeout_seconds)

    return ErrAgentConfig(cloud=cloud, local=local, local_only=local_only)
