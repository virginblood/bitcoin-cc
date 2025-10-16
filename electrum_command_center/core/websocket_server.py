"""Local WebSocket server broadcasting dispatcher events to clients."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set
from urllib.parse import parse_qs, urlparse

try:  # pragma: no cover - optional dependency guard
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:  # pragma: no cover - optional dependency guard
    websockets = None
    WebSocketServerProtocol = Any  # type: ignore[misc,assignment]

from .event_dispatcher import EventDispatcher
from .plugin_manager import PluginLoadError, PluginManager
from .telemetry_store import TelemetryStore


LOGGER = logging.getLogger(__name__)


@dataclass
class _Client:
    websocket: WebSocketServerProtocol
    queue: "asyncio.Queue[Dict[str, Any]]"
    heartbeat_task: asyncio.Task
    client_id: str


class WebSocketServer:
    """Expose dispatcher events and plugin controls over WebSocket."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        dispatcher: EventDispatcher,
        plugin_manager: PluginManager,
        allowed_tokens: Optional[Set[str]] = None,
        heartbeat_interval: float = 30.0,
        queue_maxsize: int = 100,
        telemetry_store: Optional[TelemetryStore] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._dispatcher = dispatcher
        self._plugin_manager = plugin_manager
        self._allowed_tokens = allowed_tokens or {"dev-token"}
        self._heartbeat_interval = heartbeat_interval
        self._queue_maxsize = queue_maxsize
        self._server: Optional[websockets.server.Serve] = None
        self._clients: Dict[WebSocketServerProtocol, _Client] = {}
        self._client_ids: Dict[str, WebSocketServerProtocol] = {}
        self._lock = asyncio.Lock()
        self._telemetry_store = telemetry_store

    async def start(self) -> None:
        if self._server is not None:
            return

        if websockets is None:
            raise RuntimeError(
                "The 'websockets' package is required to start the WebSocketServer."
            )

        LOGGER.info("Starting WebSocket server on ws://%s:%s", self._host, self._port)
        self._server = await websockets.serve(
            self._client_handler,
            host=self._host,
            port=self._port,
            ping_interval=None,
        )

    async def stop(self) -> None:
        if self._server is None:
            return

        LOGGER.info("Stopping WebSocket server")
        self._server.close()
        await self._server.wait_closed()
        self._server = None

        async with self._lock:
            client_websockets = list(self._clients.keys())

        for websocket in client_websockets:
            await self._disconnect_client(websocket)

    async def _client_handler(self, websocket: WebSocketServerProtocol) -> None:
        params = self._extract_params(websocket.path)
        token = params.get("token")
        if token not in self._allowed_tokens:
            LOGGER.warning("Rejecting WebSocket connection due to invalid token")
            await websocket.close(code=4401, reason="Unauthorized")
            return

        client_id = params.get("client", f"client-{id(websocket)}")
        subscriber_id = f"websocket:{client_id}"
        wildcard_queue = self._dispatcher.register_queue(
            "*", maxsize=self._queue_maxsize, subscriber_id=subscriber_id
        )

        heartbeat_task = asyncio.create_task(self._heartbeat(websocket))
        client = _Client(
            websocket=websocket,
            queue=wildcard_queue,
            heartbeat_task=heartbeat_task,
            client_id=client_id,
        )

        async with self._lock:
            existing = self._client_ids.get(client_id)
            if existing is not None and existing in self._clients:
                LOGGER.info("Replacing existing WebSocket client %s", client_id)
                await self._disconnect_client(existing)
            self._clients[websocket] = client
            self._client_ids[client_id] = websocket

        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "plugins": self._plugin_manager.plugin_status(),
                        "available_plugins": self._plugin_manager.list_available_plugins(),
                        "dispatcher": self._plugin_manager.dispatcher_metrics(),
                        "health": self._health_snapshot(),
                    }
                )
            )

            sender = asyncio.create_task(self._sender(websocket, wildcard_queue))
            receiver = asyncio.create_task(self._receiver(websocket))

            done, pending = await asyncio.wait(
                {sender, receiver, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            for task in done:
                if task is heartbeat_task:
                    continue
                exc = task.exception()
                if exc:
                    raise exc
        except Exception:
            LOGGER.exception("WebSocket client crashed")
        finally:
            await self._disconnect_client(websocket)
            await self._dispatcher.unregister_queue("*", wildcard_queue)

    async def _sender(
        self,
        websocket: WebSocketServerProtocol,
        queue: "asyncio.Queue[Dict[str, Any]]",
    ) -> None:
        while True:
            message = await queue.get()
            if message is None:
                break
            try:
                await websocket.send(json.dumps(message))
            except Exception:
                LOGGER.warning("Failed to send event to WebSocket client", exc_info=True)
                break

    async def _receiver(self, websocket: WebSocketServerProtocol) -> None:
        async for raw in websocket:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await self._send_error(websocket, "invalid_json", "Payload must be valid JSON")
                continue

            action = payload.get("action")
            if action == "ping":
                await websocket.send(json.dumps({"type": "pong"}))
                continue

            if action == "list_plugins":
                await websocket.send(json.dumps(self._plugin_listing()))
                continue

            if action == "health":
                await websocket.send(json.dumps(self._health_snapshot()))
                continue

            if action == "history":
                await websocket.send(json.dumps(self._history_snapshot()))
                continue

            if action in {"load_plugin", "unload_plugin", "reload_plugin"}:
                name = payload.get("name")
                if not name:
                    await self._send_error(
                        websocket,
                        "missing_plugin_name",
                        "Plugin management actions require a 'name' field",
                    )
                    continue

                await self._process_plugin_action(websocket, action, name)
                continue

            await self._send_error(websocket, "unknown_action", f"Action '{action}' is not supported")

    async def _process_plugin_action(
        self,
        websocket: WebSocketServerProtocol,
        action: str,
        name: str,
    ) -> None:
        try:
            if action == "load_plugin":
                await self._plugin_manager.load_plugin_by_name(name)
            elif action == "unload_plugin":
                await self._plugin_manager.unload_plugin(name)
            elif action == "reload_plugin":
                await self._plugin_manager.reload_plugin(name)
        except PluginLoadError as exc:
            await self._send_error(websocket, "plugin_error", str(exc))
        else:
            await websocket.send(json.dumps(self._plugin_listing()))

    async def _heartbeat(self, websocket: WebSocketServerProtocol) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                await websocket.ping()
        except asyncio.CancelledError:
            pass
        except Exception:
            LOGGER.debug("Heartbeat failed", exc_info=True)

    async def _disconnect_client(self, websocket: WebSocketServerProtocol) -> None:
        async with self._lock:
            client = self._clients.pop(websocket, None)
            if client:
                self._client_ids.pop(client.client_id, None)

        if client:
            client.heartbeat_task.cancel()
            try:
                client.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            try:
                await websocket.close()
            except Exception:
                LOGGER.debug("Client close failed", exc_info=True)

    def _extract_params(self, path: str) -> Dict[str, str]:
        if not path:
            return {}

        parsed = urlparse(path)
        params = parse_qs(parsed.query)
        return {key: values[0] for key, values in params.items() if values}

    def _plugin_listing(self) -> Dict[str, Any]:
        return {
            "type": "plugins",
            "plugins": self._plugin_manager.plugin_status(),
            "available_plugins": self._plugin_manager.list_available_plugins(),
            "dispatcher": self._plugin_manager.dispatcher_metrics(),
            "health": self._health_snapshot(),
        }

    def _health_snapshot(self) -> Dict[str, Any]:
        snapshot = {
            "type": "health",
            "dispatcher": self._plugin_manager.dispatcher_metrics(),
            "plugins": self._plugin_manager.plugin_health(),
            "connections": {
                "active": len(self._clients),
                "clients": [client.client_id for client in self._clients.values()],
            },
        }

        if self._telemetry_store is not None:
            self._telemetry_store.record_health_snapshot(snapshot)

        return snapshot

    def _history_snapshot(self) -> Dict[str, Any]:
        history: Dict[str, Any]
        if self._telemetry_store is None:
            history = {"health": [], "service_health": []}
        else:
            history = self._telemetry_store.history()
        return {"type": "history", **history}

    async def _send_error(
        self,
        websocket: WebSocketServerProtocol,
        code: str,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "type": "error",
            "error": {
                "code": code,
                "message": message,
            },
        }
        if details:
            payload["error"]["details"] = details
        try:
            await websocket.send(json.dumps(payload))
        except Exception:
            LOGGER.debug("Failed to send error payload", exc_info=True)
