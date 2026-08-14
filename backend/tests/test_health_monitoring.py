from backend.app.app import (
    HEALTH_CHECK_INTERVAL_SECONDS,
    should_send_discord_alert,
    build_discord_alert_message,
    list_services,
)


def test_default_health_check_interval_is_300_seconds():
    assert HEALTH_CHECK_INTERVAL_SECONDS == 300


def test_list_services_includes_last_health_status(monkeypatch):
    class FakeDB:
        def __init__(self):
            self.payloads = []

        def __getitem__(self, key):
            return self

        def find_one(self, *args, **kwargs):
            return {
                "timestamp": "2026-08-14T00:00:00Z",
                "services": [
                    {"service": "BTY Fitness", "status": "healthy", "latency_ms": 120},
                    {"service": "SAAPP Widget", "status": "down", "latency_ms": None},
                ],
            }

    monkeypatch.setattr("backend.app.app.get_db", lambda: FakeDB())
    response = list_services()

    assert response["services"][0]["status"] == "healthy"
    assert response["services"][1]["status"] == "down"


def test_should_send_discord_alert_only_for_new_down_service():
    report = {
        "overall_status": "CRITICAL",
        "services": [
            {"service": "frontend", "status": "healthy"},
            {"service": "backend", "status": "down"},
        ],
        "summary": "backend is DOWN. Overall status: CRITICAL.",
    }

    assert should_send_discord_alert(report, False) is True
    assert should_send_discord_alert(report, True) is False


def test_build_discord_alert_message_includes_down_services():
    report = {
        "overall_status": "CRITICAL",
        "services": [
            {"service": "backend", "status": "down"},
            {"service": "frontend", "status": "healthy"},
        ],
        "summary": "backend is DOWN. Overall status: CRITICAL.",
    }

    message = build_discord_alert_message(report)
    assert "erragent health alert" in message.lower()
    assert "backend" in message.lower()
    assert "CRITICAL" in message
