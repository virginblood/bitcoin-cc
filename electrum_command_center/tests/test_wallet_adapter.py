import asyncio

from electrum_command_center.core.event_dispatcher import EventDispatcher
from electrum_command_center.core.wallet_adapter import WalletAdapter


def test_wallet_adapter_emits_connection_events() -> None:
    asyncio.run(_test_wallet_adapter_emits_connection_events())


async def _test_wallet_adapter_emits_connection_events() -> None:
    dispatcher = EventDispatcher()
    wallet = WalletAdapter()
    wallet.register_event_callback(dispatcher.create_wallet_callback())

    queue = dispatcher.register_queue(
        "connection_state", subscriber_id="test-wallet-connection"
    )

    await wallet.start(server="example")

    connected = await asyncio.wait_for(queue.get(), timeout=1)
    assert connected["event"] == "connection_state"
    assert connected["data"]["state"] == "connected"
    assert connected["data"]["server"] == "example"

    await wallet.simulate_connection_state("disconnected")
    disconnected = await asyncio.wait_for(queue.get(), timeout=1)
    assert disconnected["data"]["state"] == "disconnected"
    assert disconnected["data"]["server"] == "example"

    await wallet.stop()

    await dispatcher.unregister_queue("connection_state", queue)
