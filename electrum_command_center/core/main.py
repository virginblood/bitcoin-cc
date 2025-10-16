"""Entry point used to exercise the Command Center core scaffolding."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .event_dispatcher import EventDispatcher
from .plugin_manager import PluginManager
from .wallet_adapter import WalletAdapter


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> None:
    wallet = WalletAdapter()
    dispatcher = EventDispatcher()
    wallet.register_event_callback(dispatcher.create_wallet_callback())

    plugins_root = Path(__file__).resolve().parents[1] / "plugins"
    manager = PluginManager(plugins_root, dispatcher, dev_mode=True)

    await wallet.start()
    await manager.load_plugins()

    # Simulate a fake transaction so we can observe plugin dispatch.
    await wallet.simulate_transaction_received(
        txid="fake_txid_123",
        amount_sats=2500,
        memo="Thanks for the stream!",
    )

    await asyncio.sleep(0.1)
    await manager.shutdown()
    await wallet.stop()


if __name__ == "__main__":
    asyncio.run(main())
