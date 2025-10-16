"""Helpers for bootstrapping a real Electrum runtime."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

try:  # pragma: no cover - optional Electrum dependency
    from electrum import constants
    from electrum.daemon import Daemon
    from electrum.network import Network
    from electrum.simple_config import SimpleConfig
    from electrum.util import create_and_start_event_loop
    from electrum.wallet import Abstract_Wallet as Wallet
except ImportError:  # pragma: no cover - simulated environments
    constants = None  # type: ignore[assignment]
    Daemon = None  # type: ignore[assignment]
    Network = None  # type: ignore[assignment]
    SimpleConfig = None  # type: ignore[assignment]
    create_and_start_event_loop = None  # type: ignore[assignment]
    Wallet = None  # type: ignore[assignment]


class ElectrumRuntimeError(RuntimeError):
    """Raised when the Electrum runtime cannot be created."""


@dataclass
class ElectrumRuntime:
    """Container bundling Electrum objects started for the adapter."""

    config: SimpleConfig
    loop: asyncio.AbstractEventLoop
    stopping_future: asyncio.Future[Any]
    loop_thread: threading.Thread
    daemon: Daemon
    wallet: Wallet
    network: Optional[Network]
    _stopped: bool = False

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Tear down Electrum threads and wait for a clean shutdown."""

        if self._stopped:
            return

        if not self.stopping_future.done():
            self.stopping_future.set_result(1)

        async def _shutdown() -> None:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self.daemon.stop(), self.loop)
            )

        try:
            await asyncio.wait_for(_shutdown(), timeout=timeout)
        finally:
            self.loop_thread.join(timeout=timeout)
            self._stopped = True


def start_electrum_runtime(
    wallet_path: str,
    *,
    password: Optional[str] = None,
    config_overrides: Optional[Mapping[str, Any]] = None,
    testnet: bool = False,
) -> ElectrumRuntime:
    """Spin up a lightweight Electrum daemon and load ``wallet_path``.

    The returned :class:`ElectrumRuntime` instance owns the Electrum daemon, the
    background network thread and the opened wallet.  Call :meth:`ElectrumRuntime.stop`
    when the integration is no longer required.
    """

    if constants is None or Daemon is None or SimpleConfig is None:
        raise ElectrumRuntimeError(
            "Electrum runtime unavailable – install Electrum dependencies to enable integration"
        )

    config_dict: Dict[str, Any] = {"wallet_path": wallet_path}
    if config_overrides:
        config_dict.update(dict(config_overrides))

    if testnet:
        constants.BitcoinTestnet.set_as_network()
    else:
        constants.BitcoinMainnet.set_as_network()

    config = SimpleConfig(config_dict)

    loop, stopping_future, loop_thread = _create_loop()

    try:
        daemon = Daemon(config, listen_jsonrpc=False)
    except Exception as exc:  # pragma: no cover - defensive logging
        raise ElectrumRuntimeError("Failed to start Electrum daemon") from exc

    wallet = daemon.load_wallet(wallet_path, password=password, upgrade=True)
    if wallet is None:
        raise ElectrumRuntimeError(f"Wallet at '{wallet_path}' could not be opened")

    network = daemon.network

    return ElectrumRuntime(
        config=config,
        loop=loop,
        stopping_future=stopping_future,
        loop_thread=loop_thread,
        daemon=daemon,
        wallet=wallet,
        network=network,
    )


def _create_loop() -> Tuple[asyncio.AbstractEventLoop, asyncio.Future[Any], threading.Thread]:
    if create_and_start_event_loop is None:
        raise ElectrumRuntimeError("Electrum event loop helper is unavailable")
    return create_and_start_event_loop()

