"""Entry point used to exercise the Command Center core scaffolding."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .event_dispatcher import EventDispatcher
from .plugin_manager import PluginManager
from .wallet_adapter import WalletAdapter
from .websocket_server import WebSocketServer


LOGGER = logging.getLogger(__name__)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> None:
    wallet = WalletAdapter(metadata={"source": "simulation"})
    dispatcher = EventDispatcher(
        default_queue_size=64,
        overflow_strategy="drop_oldest",
    )
    wallet.register_event_callback(dispatcher.create_wallet_callback())

    plugins_root = Path(__file__).resolve().parents[1] / "plugins"
    manager = PluginManager(
        plugins_root,
        dispatcher,
        dev_mode=True,
        queue_maxsize=64,
    )

    await wallet.start()
    await manager.load_plugins()
    await manager.start_watcher()
    LOGGER.info("Loaded plugins: %s", manager.plugin_status())

    websocket_server = WebSocketServer(
        host="127.0.0.1",
        port=8765,
        dispatcher=dispatcher,
        plugin_manager=manager,
        allowed_tokens={"dev-token"},
        heartbeat_interval=10.0,
    )
    try:
        await websocket_server.start()
    except RuntimeError as exc:
        LOGGER.warning("WebSocket server not started: %s", exc)

    # Simulate a fake transaction so we can observe plugin dispatch.
    await wallet.simulate_transaction_received(
        txid="fake_txid_123",
        amount_sats=2500,
        memo="Thanks for the stream!",
    )

    await asyncio.sleep(0.1)
    LOGGER.info("Dispatcher metrics snapshot: %s", dispatcher.metrics_snapshot())
    LOGGER.info("Dispatcher subscribers: %s", dispatcher.subscriber_snapshot())
    LOGGER.info("Plugin health snapshot: %s", manager.plugin_health())
    await websocket_server.stop()
    await manager.shutdown()
    await wallet.stop()


if __name__ == "__main__":
    asyncio.run(main())
