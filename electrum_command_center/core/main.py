"""Entry point used to exercise the Command Center core scaffolding."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .electrum_runtime import ElectrumRuntime, ElectrumRuntimeError, start_electrum_runtime
from .event_dispatcher import EventDispatcher
from .plugin_manager import PluginManager
from .service_health import ServiceHealthMonitor
from .wallet_adapter import WalletAdapter
from .websocket_server import WebSocketServer


LOGGER = logging.getLogger(__name__)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Electrum Command Center demo")
    parser.add_argument(
        "--wallet-path",
        default=os.environ.get("ECC_WALLET_PATH"),
        help="Path to an Electrum wallet file to attach to the adapter",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        default=_env_flag("ECC_TESTNET"),
        help="Use Electrum testnet parameters",
    )
    parser.add_argument(
        "--wallet-password",
        default=os.environ.get("ECC_WALLET_PASSWORD"),
        help="Password used to unlock the Electrum wallet",
    )
    parser.add_argument(
        "--electrum-dir",
        default=os.environ.get("ECC_ELECTRUM_DIR"),
        help="Override Electrum's data directory",
    )
    return parser.parse_args()


async def _maybe_bootstrap_wallet(args: argparse.Namespace, wallet: WalletAdapter) -> Optional[ElectrumRuntime]:
    if not args.wallet_path:
        LOGGER.info("No Electrum wallet configured – running in simulation mode")
        return None

    loop = asyncio.get_running_loop()
    overrides: Dict[str, Any] = {}
    if args.electrum_dir:
        overrides["electrum_path"] = args.electrum_dir

    try:
        runtime = await loop.run_in_executor(
            None,
            lambda: start_electrum_runtime(
                args.wallet_path,
                password=args.wallet_password,
                config_overrides=overrides,
                testnet=args.testnet,
            ),
        )
    except ElectrumRuntimeError as exc:
        LOGGER.error("Failed to attach Electrum wallet: %s", exc)
        return None

    wallet.set_wallet(runtime.wallet)
    if runtime.network is not None:
        wallet.set_network(runtime.network)

    LOGGER.info("Attached Electrum wallet at %s", args.wallet_path)
    return runtime


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def main() -> None:
    args = _parse_args()

    runtime: Optional[ElectrumRuntime] = None
    metadata: Dict[str, Any] = {"source": "simulation"}
    wallet = WalletAdapter(metadata=metadata)
    dispatcher = EventDispatcher(
        default_queue_size=64,
        overflow_strategy="drop_oldest",
    )
    wallet.register_event_callback(dispatcher.create_wallet_callback())
    service_monitor = ServiceHealthMonitor(dispatcher)

    plugins_root = Path(__file__).resolve().parents[1] / "plugins"
    manager = PluginManager(
        plugins_root,
        dispatcher,
        dev_mode=True,
        queue_maxsize=64,
    )

    runtime = await _maybe_bootstrap_wallet(args, wallet)
    if runtime is not None:
        metadata = {
            "source": "electrum",
            "wallet_path": args.wallet_path,
            "network": "testnet" if args.testnet else "mainnet",
        }
        wallet.set_metadata(metadata)

    await wallet.start()
    await service_monitor.start()
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
        service_monitor=service_monitor,
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
    LOGGER.info("Service health snapshot: %s", service_monitor.snapshot())
    await websocket_server.stop()
    await manager.shutdown()
    await service_monitor.stop()
    if runtime is not None:
        await runtime.stop()
    await wallet.stop()


if __name__ == "__main__":
    asyncio.run(main())
