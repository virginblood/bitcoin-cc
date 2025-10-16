import asyncio

from ..core.event_dispatcher import EventDispatcher


def test_wildcard_subscription_receives_events() -> None:
    asyncio.run(_test_wildcard_subscription())


async def _test_wildcard_subscription() -> None:
    dispatcher = EventDispatcher()
    queue = dispatcher.register_queue("*", subscriber_id="test-client")

    await dispatcher.emit("transaction_received", {"amount": 1})
    message = await asyncio.wait_for(queue.get(), timeout=1)

    assert message["event"] == "transaction_received"
    assert message["data"]["amount"] == 1


def test_drop_new_strategy_counts_dropped_events() -> None:
    asyncio.run(_test_drop_new_strategy_counts_dropped_events())


async def _test_drop_new_strategy_counts_dropped_events() -> None:
    dispatcher = EventDispatcher(default_queue_size=1, overflow_strategy="drop_new")
    queue = dispatcher.register_queue("balance_updated", subscriber_id="drop-new")

    await dispatcher.emit("balance_updated", {"value": 1})
    await dispatcher.emit("balance_updated", {"value": 2})

    stored = await asyncio.wait_for(queue.get(), timeout=1)
    assert stored["data"]["value"] == 1

    metrics = dispatcher.metrics_snapshot()
    assert metrics["delivered"]["balance_updated"] == 1
    assert metrics["dropped"]["balance_updated"] == 1


def test_drop_oldest_strategy_keeps_latest_event() -> None:
    asyncio.run(_test_drop_oldest_strategy_keeps_latest_event())


async def _test_drop_oldest_strategy_keeps_latest_event() -> None:
    dispatcher = EventDispatcher(default_queue_size=1, overflow_strategy="drop_oldest")
    queue = dispatcher.register_queue("balance_updated", subscriber_id="drop-oldest")

    await dispatcher.emit("balance_updated", {"value": 1})
    await dispatcher.emit("balance_updated", {"value": 2})

    stored = await asyncio.wait_for(queue.get(), timeout=1)
    assert stored["data"]["value"] == 2
