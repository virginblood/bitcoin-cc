from electrum_command_center.core.telemetry_store import TelemetryStore


def test_telemetry_store_limits_history() -> None:
    store = TelemetryStore(max_health_records=2, max_service_records=1)

    store.record_health_snapshot({"value": 1})
    store.record_health_snapshot({"value": 2})
    store.record_health_snapshot({"value": 3})

    health = store.health_history()
    assert len(health) == 2
    assert [entry["payload"]["value"] for entry in health] == [2, 3]

    store.record_service_health({"state": "unknown"})
    store.record_service_health({"state": "connected"})

    service = store.service_history()
    assert len(service) == 1
    assert service[0]["payload"]["state"] == "connected"
