"""Stream bridge plugin powering overlay broadcasts."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set
from urllib.parse import parse_qs, urlparse

try:  # pragma: no cover - optional dependency guard
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:  # pragma: no cover - optional dependency guard
    websockets = None
    WebSocketServerProtocol = Any  # type: ignore[misc,assignment]

from electrum_command_center.core.plugin_context import PluginContext


LOGGER = logging.getLogger(__name__)

PLUGIN_NAME = "stream_bridge"
EVENT_SUBSCRIPTIONS = [
    "transaction_received",
    "transaction_confirmed",
    "automation.action",
]

_CONFIG_FILENAME = "stream_bridge.json"
_DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 8788,
    "allowed_origins": [],
    "token": "overlay-token",
}


@dataclass
class _OverlayServer:
    host: str
    port: int
    allowed_origins: Set[str] = field(default_factory=set)
    token: Optional[str] = None
    _server: Optional[websockets.server.Serve] = None
    _clients: Dict[WebSocketServerProtocol, str] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def start(self) -> None:
        if websockets is None:
            raise RuntimeError(
                "The 'websockets' package is required for the stream bridge plugin"
            )

        if self._server is not None:
            return

        LOGGER.info("Stream bridge overlay listening on ws://%s:%s", self.host, self.port)
        self._server = await websockets.serve(
            self._handle_client,
            host=self.host,
            port=self.port,
            ping_interval=20.0,
        )

    async def stop(self) -> None:
        if self._server is None:
            return

        LOGGER.info("Stopping stream bridge overlay server")
        self._server.close()
        await self._server.wait_closed()
        self._server = None

        async with self._lock:
            clients = list(self._clients)
            self._clients.clear()

        for client in clients:
            try:
                await client.close()
            except Exception:  # pragma: no cover - defensive logging
                LOGGER.debug("Failed to close overlay client", exc_info=True)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        if not self._clients:
            return

        payload = json.dumps(message)
        dead: Set[WebSocketServerProtocol] = set()

        async with self._lock:
            clients = list(self._clients.keys())

        for websocket in clients:
            try:
                await websocket.send(payload)
            except Exception:
                LOGGER.debug("Overlay client send failed", exc_info=True)
                dead.add(websocket)

        for websocket in dead:
            await self._disconnect(websocket)

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        origin = websocket.request_headers.get("Origin") if hasattr(websocket, "request_headers") else None
        if self.allowed_origins and origin not in self.allowed_origins:
            LOGGER.warning("Rejecting overlay client due to origin '%s'", origin)
            await websocket.close(code=4403, reason="Forbidden")
            return

        params = _extract_params(websocket.path)
        token = params.get("token")
        client_id = params.get("client", f"overlay-{id(websocket)}")

        if self.token and token != self.token:
            LOGGER.warning("Rejecting overlay client due to invalid token")
            await websocket.close(code=4401, reason="Unauthorized")
            return

        async with self._lock:
            if client_id in self._clients.values():
                to_drop = [ws for ws, cid in self._clients.items() if cid == client_id]
                for ws in to_drop:
                    await self._disconnect(ws)
            self._clients[websocket] = client_id

        LOGGER.info(
            "Overlay client connected (%d active id=%s)", len(self._clients), client_id
        )

        try:
            async for _ in websocket:
                # Overlay clients are currently receive-only; we simply drain
                # incoming data to keep the connection alive.
                await asyncio.sleep(0)
        except Exception:
            LOGGER.debug("Overlay client errored", exc_info=True)
        finally:
            await self._disconnect(websocket)

    async def _disconnect(self, websocket: WebSocketServerProtocol) -> None:
        async with self._lock:
            self._clients.pop(websocket, None)

        try:
            await websocket.close()
        except Exception:
            LOGGER.debug("Overlay client close failed", exc_info=True)

        LOGGER.info("Overlay client disconnected (%d active)", len(self._clients))


_SERVER: Optional[_OverlayServer] = None


async def on_startup(context: PluginContext) -> None:
    """Initialise the overlay broadcast server."""

    global _SERVER

    config = _load_config(context.config_dir / _CONFIG_FILENAME)

    if websockets is None:
        LOGGER.warning(
            "stream_bridge plugin disabled because the 'websockets' dependency is missing"
        )
        return

    _SERVER = _OverlayServer(
        host=config.get("host", _DEFAULT_CONFIG["host"]),
        port=int(config.get("port", _DEFAULT_CONFIG["port"])),
        allowed_origins=set(config.get("allowed_origins", [])),
    )
    await _SERVER.start()


async def on_event(event_name: str, payload: Dict[str, Any]) -> None:
    if _SERVER is None:
        LOGGER.debug("Overlay server not running; dropping event %s", event_name)
        return

    if event_name == "automation.action":
        message = {
            "type": "automation",
            "action": payload.get("action"),
            "data": payload,
        }
    else:
        message = {
            "type": "wallet_event",
            "event": event_name,
            "data": {
                "txid": payload.get("txid"),
                "amount_sats": payload.get("amount_sats"),
                "memo": payload.get("memo"),
                "confirmations": payload.get("confirmations"),
            },
            "display": _format_display(payload),
        }

    await _SERVER.broadcast(message)


async def on_shutdown(context: PluginContext) -> None:
    global _SERVER

    if _SERVER is not None:
        await _SERVER.stop()
        _SERVER = None


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(_DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(_DEFAULT_CONFIG)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOGGER.warning("Invalid stream bridge config at %s; using defaults", path)
        return dict(_DEFAULT_CONFIG)

    merged = dict(_DEFAULT_CONFIG)
    merged.update({k: v for k, v in data.items() if k in _DEFAULT_CONFIG})
    return merged


def _extract_params(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}

    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return {key: values[0] for key, values in params.items() if values}


def _format_display(payload: Dict[str, Any]) -> Dict[str, Any]:
    amount = payload.get("amount_sats")
    memo = payload.get("memo", "")
    txid = payload.get("txid", "")
    return {
        "headline": f"{amount} sats received" if amount is not None else "New transaction",
        "memo": memo,
        "txid": txid[-12:] if txid else None,
    }

