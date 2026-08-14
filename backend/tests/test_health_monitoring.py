from backend.app.app import HEALTH_CHECK_INTERVAL_SECONDS


def test_default_health_check_interval_is_300_seconds():
    assert HEALTH_CHECK_INTERVAL_SECONDS == 300
