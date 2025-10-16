import asyncio
import contextlib

from electrum_command_center.core.websocket_server import WebSocketServer, _Client


class _DummyDispatcher:
    """Minimal dispatcher stub for testing."""


class _DummyPluginManager:
    """Minimal plugin manager stub for testing."""


class _DummyServer:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class _DummyWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _run_stop_without_deadlock() -> None:
    server = WebSocketServer(
        host="127.0.0.1",
        port=0,
        dispatcher=_DummyDispatcher(),
        plugin_manager=_DummyPluginManager(),
    )

    queue = asyncio.Queue()
    heartbeat_task = asyncio.create_task(asyncio.sleep(3600))
    websocket = _DummyWebSocket()
    client = _Client(
        websocket=websocket,
        queue=queue,
        heartbeat_task=heartbeat_task,
        client_id="client-1",
    )

    async with server._lock:
        server._clients[websocket] = client
        server._client_ids[client.client_id] = websocket

    server._server = _DummyServer()

    await asyncio.wait_for(server.stop(), timeout=1)

    assert websocket.closed
    assert server._server is None
    assert not server._clients
    assert not server._client_ids
    assert queue.get_nowait() is None

    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat_task


def test_websocket_server_stop_completes() -> None:
    asyncio.run(_run_stop_without_deadlock())
