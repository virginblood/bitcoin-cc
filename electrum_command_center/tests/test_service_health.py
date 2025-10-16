import asyncio

from electrum_command_center.core.event_dispatcher import EventDispatcher
from electrum_command_center.core.service_health import ServiceHealthMonitor
from electrum_command_center.core.telemetry_store import TelemetryStore
from electrum_command_center.core.wallet_adapter import WalletAdapter


def test_service_monitor_emits_health_events() -> None:
    asyncio.run(_test_service_monitor_emits_health_events())


async def _test_service_monitor_emits_health_events() -> None:
    dispatcher = EventDispatcher()
    telemetry = TelemetryStore(max_health_records=5, max_service_records=5)
    monitor = ServiceHealthMonitor(dispatcher, telemetry_store=telemetry)

    queue = dispatcher.register_queue(
        "service_health", subscriber_id="test-service-monitor"
    )

    await monitor.start()

    initial = await asyncio.wait_for(queue.get(), timeout=1)
    assert initial["event"] == "service_health"
    assert initial["data"]["severity"] == "degraded"
    assert initial["data"]["state"] == "unknown"

    await dispatcher.emit(
        "connection_state", {"state": "connected", "server": "electrum.example"}
    )

    recovered = await asyncio.wait_for(queue.get(), timeout=1)
    assert recovered["data"]["severity"] == "ok"
    assert recovered["data"]["server"] == "electrum.example"

    snapshot = monitor.snapshot()
    assert snapshot["network"]["state"] == "connected"
    assert snapshot["network"]["severity"] == "ok"

    history = telemetry.service_history()
    assert len(history) >= 2
    assert history[0]["payload"]["state"] == "unknown"
    assert history[-1]["payload"]["state"] == "connected"

    await monitor.stop()
    await dispatcher.unregister_queue("service_health", queue)


def test_service_monitor_tracks_wallet_adapter() -> None:
    asyncio.run(_test_service_monitor_tracks_wallet_adapter())


async def _test_service_monitor_tracks_wallet_adapter() -> None:
    dispatcher = EventDispatcher()
    telemetry = TelemetryStore(max_health_records=5, max_service_records=5)
    wallet = WalletAdapter()
    wallet.register_event_callback(dispatcher.create_wallet_callback())
    monitor = ServiceHealthMonitor(dispatcher, telemetry_store=telemetry)

    queue = dispatcher.register_queue(
        "service_health", subscriber_id="test-wallet-monitor"
    )

    await monitor.start()

    initial = await asyncio.wait_for(queue.get(), timeout=1)
    assert initial["data"]["state"] == "unknown"

    await wallet.start(server="electrum.test")

    connected = await asyncio.wait_for(queue.get(), timeout=1)
    assert connected["data"]["state"] == "connected"
    assert connected["data"]["server"] == "electrum.test"

    await wallet.simulate_connection_state("disconnected")
    degraded = await asyncio.wait_for(queue.get(), timeout=1)
    assert degraded["data"]["state"] == "disconnected"

    await wallet.stop()

    await monitor.stop()
    await dispatcher.unregister_queue("service_health", queue)
