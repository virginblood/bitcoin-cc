from __future__ import annotations

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from electrum_command_center.core import electrum_runtime


class _DummyThread:
    def __init__(self) -> None:
        self.join_called = False

    def join(self, timeout: float | None = None) -> None:  # pragma: no cover - signature parity
        self.join_called = True


def _patch_loop(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.AbstractEventLoop, asyncio.Future[Any], _DummyThread]:
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    thread = _DummyThread()
    monkeypatch.setattr(electrum_runtime, "_create_loop", lambda: (loop, fut, thread))
    return loop, fut, thread


def test_start_runtime_uses_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, _, thread = _patch_loop(monkeypatch)

    captured: Dict[str, Any] = {}
    monkeypatch.setattr(
        electrum_runtime,
        "SimpleConfig",
        lambda data: captured.setdefault("config", data) or SimpleNamespace(),
    )

    wallet = SimpleNamespace()
    daemon = SimpleNamespace(load_wallet=lambda *a, **k: wallet, network=SimpleNamespace())
    monkeypatch.setattr(electrum_runtime, "Daemon", lambda *a, **k: daemon)

    runtime = electrum_runtime.start_electrum_runtime(
        "wallet-path",
        config_overrides={"foo": "bar"},
        testnet=True,
    )

    assert runtime.wallet is wallet
    assert captured["config"]["foo"] == "bar"
    assert captured["config"]["wallet_path"] == "wallet-path"

    loop.close()
    assert thread.join_called is False


def test_runtime_stop_triggers_daemon_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    loop, stopping_future, thread = _patch_loop(monkeypatch)

    def _stop() -> None:
        if not stopping_future.done():
            stopping_future.set_result(1)

    daemon = SimpleNamespace(network=None)
    daemon.load_wallet = lambda *a, **k: SimpleNamespace()
    daemon.stop = _stop
    monkeypatch.setattr(electrum_runtime, "Daemon", lambda *a, **k: daemon)
    monkeypatch.setattr(electrum_runtime, "SimpleConfig", lambda data: SimpleNamespace())

    fut: Future[None] = Future()
    fut.set_result(None)

    def _fake_run_coroutine_threadsafe(coro, loop_obj):
        assert coro is None
        return fut

    monkeypatch.setattr(
        electrum_runtime.asyncio,
        "run_coroutine_threadsafe",
        _fake_run_coroutine_threadsafe,
    )

    runtime = electrum_runtime.start_electrum_runtime("wallet-path")

    asyncio.run(runtime.stop())

    assert thread.join_called is True
    assert stopping_future.done()

    loop.close()
