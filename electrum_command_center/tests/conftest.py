"""Test configuration for Electrum Command Center."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace


def _ensure_module(name: str) -> ModuleType:
    module = ModuleType(name)
    sys.modules[name] = module
    return module


if "electrum" not in sys.modules:
    electrum_mod = _ensure_module("electrum")

    constants_mod = _ensure_module("electrum.constants")

    class _Net:
        @staticmethod
        def set_as_network() -> None:
            return None

    constants_mod.BitcoinTestnet = _Net
    constants_mod.BitcoinMainnet = _Net
    electrum_mod.constants = constants_mod

    daemon_mod = _ensure_module("electrum.daemon")

    class _Daemon:
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - simple stub
            pass

        def load_wallet(self, *args, **kwargs):  # pragma: no cover - simple stub
            return None

    daemon_mod.Daemon = _Daemon
    electrum_mod.daemon = daemon_mod

    network_mod = _ensure_module("electrum.network")
    network_mod.Network = SimpleNamespace
    electrum_mod.network = network_mod

    simple_config_mod = _ensure_module("electrum.simple_config")

    class _SimpleConfig(dict):
        pass

    simple_config_mod.SimpleConfig = _SimpleConfig
    electrum_mod.simple_config = simple_config_mod

    util_mod = _ensure_module("electrum.util")

    def _create_and_start_event_loop():
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        thread = threading.Thread(target=lambda: None)
        return loop, future, thread

    util_mod.create_and_start_event_loop = _create_and_start_event_loop
    electrum_mod.util = util_mod

    wallet_mod = _ensure_module("electrum.wallet")

    class _Wallet:
        pass

    wallet_mod.Abstract_Wallet = _Wallet
    electrum_mod.wallet = wallet_mod
