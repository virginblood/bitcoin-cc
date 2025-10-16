import asyncio

from electrum_command_center.core.event_dispatcher import EventDispatcher
from electrum_command_center.core.service_health import ServiceHealthMonitor


def test_service_monitor_emits_health_events() -> None:
    asyncio.run(_test_service_monitor_emits_health_events())


async def _test_service_monitor_emits_health_events() -> None:
    dispatcher = EventDispatcher()
    monitor = ServiceHealthMonitor(dispatcher)

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

    await monitor.stop()
    await dispatcher.unregister_queue("service_health", queue)
