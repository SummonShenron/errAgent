from erragent.config import CloudConfig, resolve_cloud_credentials


def _cloud(**overrides) -> CloudConfig:
    defaults = dict(
        url="https://erragent.example",
        service="svc",
        app_id=None,
        app_secret=None,
        ingest_secret=None,
        timeout_seconds=30.0,
    )
    defaults.update(overrides)
    return CloudConfig(**defaults)


def test_uses_app_credentials_when_both_present():
    cloud = _cloud(app_id="bty", app_secret="app-secret", ingest_secret="legacy")
    assert resolve_cloud_credentials(cloud) == ("bty", "app-secret")


def test_falls_back_to_legacy_secret_when_app_secret_missing():
    # Regression: a partially-migrated config (ERRAGENT_APP_ID set, ERRAGENT_APP_SECRET not
    # yet set — e.g. BTY's real .env) must NOT send x-app-id without a matching secret, since
    # authenticate_ingest_client() rejects that combination outright rather than falling back
    # to the legacy secret once x-app-id is present. Must fall back to legacy-secret-only.
    cloud = _cloud(app_id="bty", app_secret=None, ingest_secret="legacy")
    assert resolve_cloud_credentials(cloud) == (None, "legacy")


def test_falls_back_to_legacy_secret_when_app_id_missing():
    cloud = _cloud(app_id=None, app_secret="app-secret", ingest_secret="legacy")
    assert resolve_cloud_credentials(cloud) == (None, "legacy")


def test_no_credentials_configured():
    cloud = _cloud()
    assert resolve_cloud_credentials(cloud) == (None, None)
