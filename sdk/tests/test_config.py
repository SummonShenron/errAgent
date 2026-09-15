from erragent.config import CloudConfig, load_config, resolve_cloud_credentials


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


def _set_common_env(monkeypatch):
    monkeypatch.setenv("ERRAGENT_SERVICE", "svc")
    monkeypatch.setenv("ERRAGENT_URL", "https://erragent.example")
    monkeypatch.setenv("ERRAGENT_INGEST_SECRET", "secret")
    monkeypatch.delenv("ERRAGENT_APP_ID", raising=False)
    monkeypatch.delenv("ERRAGENT_APP_SECRET", raising=False)


def test_local_only_defaults_true_when_local_url_is_set(monkeypatch):
    # Regression: local-dev mode used to dual-report (install both a cloud and a local handler)
    # by default. Live testing showed this reports every local error twice — once via the
    # daemon (works) and once via a direct cloud report that's guaranteed to fail analysis,
    # since the cloud pipeline can't fetch an uncommitted local-only fix from GitHub. The
    # daemon already forwards to the cloud itself, so defaulting to local-only doesn't lose
    # incident visibility — it just stops the guaranteed-to-fail duplicate.
    _set_common_env(monkeypatch)
    monkeypatch.setenv("ERRAGENT_LOCAL_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("ERRAGENT_LOCAL_ONLY", raising=False)

    config = load_config()

    assert config.local_only is True
    assert config.cloud is None
    assert config.local is not None


def test_local_only_false_opts_back_into_dual_report(monkeypatch):
    _set_common_env(monkeypatch)
    monkeypatch.setenv("ERRAGENT_LOCAL_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ERRAGENT_LOCAL_ONLY", "false")

    config = load_config()

    assert config.local_only is False
    assert config.cloud is not None
    assert config.local is not None


def test_local_only_irrelevant_when_local_url_not_set(monkeypatch):
    _set_common_env(monkeypatch)
    monkeypatch.delenv("ERRAGENT_LOCAL_URL", raising=False)
    monkeypatch.delenv("ERRAGENT_LOCAL_ONLY", raising=False)

    config = load_config()

    assert config.local_only is False
    assert config.cloud is not None
    assert config.local is None
