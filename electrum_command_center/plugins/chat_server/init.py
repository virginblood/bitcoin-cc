"""Local chat server plugin exposing a simple WebSocket + HTML UI."""

from __future__ import annotations

import asyncio
import json
import logging
from itertools import count
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

try:  # pragma: no cover - optional dependency guard
    import websockets
    from websockets.server import WebSocketServerProtocol
except ImportError:  # pragma: no cover - optional dependency guard
    websockets = None
    WebSocketServerProtocol = Any  # type: ignore[misc,assignment]

from electrum_command_center.core.plugin_context import PluginContext


LOGGER = logging.getLogger(__name__)

PLUGIN_NAME = "chat_server"
EVENT_SUBSCRIPTIONS = ["transaction_received", "automation.action"]

_CONFIG_FILENAME = "chat_server.json"
_DEFAULT_CONFIG = {
    "ws_host": "127.0.0.1",
    "ws_port": 8790,
    "http_host": "127.0.0.1",
    "http_port": 8088,
    "room_name": "Command Center Chat",
    "token": "chat-token",
}

_HTML_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>{room_name}</title>
    <style>
      body {{ font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 0; }}
      header {{ padding: 1rem; background: #20232a; font-size: 1.2rem; font-weight: bold; }}
      #messages {{ list-style: none; padding: 1rem; margin: 0; max-height: 70vh; overflow-y: auto; }}
      #messages li {{ margin-bottom: 0.5rem; }}
      #messages li.system {{ color: #61dafb; }}
      #messages li.tip {{ color: #ffd166; }}
      form {{ display: flex; padding: 1rem; gap: 0.5rem; background: #20232a; }}
      input[type="text"] {{ flex: 1; padding: 0.5rem; border-radius: 4px; border: none; }}
      button {{ padding: 0.5rem 1rem; border: none; border-radius: 4px; background: #61dafb; color: #111; font-weight: bold; }}
    </style>
  </head>
  <body>
    <header>{room_name}</header>
    <ul id="messages"></ul>
    <form id="chat-form">
      <input type="text" id="chat-input" autocomplete="off" placeholder="Say something…" />
      <button type="submit">Send</button>
    </form>
    <script>
      const messages = document.getElementById('messages');
      const form = document.getElementById('chat-form');
      const input = document.getElementById('chat-input');
      const ws = new WebSocket('{ws_url}');

      function appendMessage(text, cssClass) {{
        const node = document.createElement('li');
        node.textContent = text;
        if (cssClass) {{ node.classList.add(cssClass); }}
        messages.appendChild(node);
        messages.scrollTop = messages.scrollHeight;
      }}

      ws.onmessage = (event) => {{
        try {{
          const payload = JSON.parse(event.data);
          if (payload.type === 'chat') {{
            appendMessage(`${{payload.user}}: ${{payload.message}}`);
          }} else if (payload.type === 'system') {{
            appendMessage(payload.message, 'system');
          }} else if (payload.type === 'tip') {{
            const tip = payload.data;
            const memo = tip.memo ? ` — ${tip.memo}` : '';
            appendMessage(`⚡ Tip: ${{tip.amount_sats}} sats from ${tip.txid}${memo}`, 'tip');
          }} else if (payload.type === 'error') {{
            appendMessage(`Error: ${{payload.error}}`, 'system');
          }}
        }} catch (err) {{
          console.error('Invalid payload', err);
        }}
      }};

      ws.onopen = () => appendMessage('Connected to chat', 'system');
      ws.onclose = () => appendMessage('Disconnected from chat', 'system');

      form.addEventListener('submit', (event) => {{
        event.preventDefault();
        const text = input.value.trim();
        if (!text) {{ return; }}
        ws.send(JSON.stringify({{ type: 'chat', message: text }}));
        input.value = '';
      }});
    </script>
  </body>
</html>
"""


class _ChatWebSocketServer:
    def __init__(self, host: str, port: int, room_name: str, *, token: Optional[str]) -> None:
        self._host = host
        self._port = port
        self._room_name = room_name
        self._token = token
        self._server: Optional[websockets.server.Serve] = None
        self._clients: Dict[WebSocketServerProtocol, str] = {}
        self._client_ids: Dict[str, WebSocketServerProtocol] = {}
        self._id_counter = count(1)
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if websockets is None:
            raise RuntimeError(
                "The 'websockets' package is required for the chat_server plugin"
            )

        if self._server is not None:
            return

        LOGGER.info(
            "Chat WebSocket listening on ws://%s:%s (room: %s)",
            self._host,
            self._port,
            self._room_name,
        )
        self._server = await websockets.serve(
            self._handle_client,
            host=self._host,
            port=self._port,
            ping_interval=20.0,
        )

    async def stop(self) -> None:
        if self._server is None:
            return

        LOGGER.info("Stopping chat WebSocket server")
        self._server.close()
        await self._server.wait_closed()
        self._server = None

        async with self._lock:
            clients = list(self._clients.keys())
            self._clients.clear()

        for websocket in clients:
            try:
                await websocket.close()
            except Exception:  # pragma: no cover - defensive logging
                LOGGER.debug("Failed to close chat client", exc_info=True)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        message = json.dumps(payload)

        async with self._lock:
            clients = list(self._clients.keys())

        if not clients:
            return

        dead: list[WebSocketServerProtocol] = []
        for websocket in clients:
            try:
                await websocket.send(message)
            except Exception:
                LOGGER.debug("Chat client send failed", exc_info=True)
                dead.append(websocket)

        for websocket in dead:
            await self._disconnect(websocket)

    async def system_message(self, message: str) -> None:
        await self.broadcast({"type": "system", "message": message})

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        params = _extract_params(websocket.path)
        token = params.get("token")
        client_id = params.get("client", f"guest-{next(self._id_counter)}")

        if self._token and token != self._token:
            LOGGER.warning("Rejecting chat client due to invalid token")
            await websocket.close(code=4401, reason="Unauthorized")
            return

        username = client_id

        async with self._lock:
            previous = self._client_ids.get(client_id)
            if previous is not None and previous in self._clients:
                await self._disconnect(previous)
            self._clients[websocket] = username
            self._client_ids[client_id] = websocket

        await self.system_message(f"{username} joined the chat")

        try:
            async for raw in websocket:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_error(websocket, "invalid_json", "Payload must be valid JSON")
                    continue

                if payload.get("type") != "chat":
                    await _send_error(
                        websocket,
                        "unknown_type",
                        "Only chat messages are accepted",
                    )
                    continue

                text = str(payload.get("message", "")).strip()
                if not text:
                    continue

                await self.broadcast(
                    {"type": "chat", "user": username, "message": text}
                )
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.debug("Chat client crashed", exc_info=True)
        finally:
            await self._disconnect(websocket)

    async def _disconnect(self, websocket: WebSocketServerProtocol) -> None:
        async with self._lock:
            username = self._clients.pop(websocket, None)
            if username:
                self._client_ids.pop(username, None)

        if username:
            await self.system_message(f"{username} left the chat")

        try:
            await websocket.close()
        except Exception:
            LOGGER.debug("Chat client close failed", exc_info=True)


class _StaticHTTPServer:
    def __init__(self, host: str, port: int, html: str) -> None:
        self._host = host
        self._port = port
        self._html = html.encode("utf-8")
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        if self._server is not None:
            return

        LOGGER.info("Chat HTTP UI available at http://%s:%s", self._host, self._port)
        self._server = await asyncio.start_server(
            self._handle_request, host=self._host, port=self._port
        )

    async def stop(self) -> None:
        if self._server is None:
            return

        LOGGER.info("Stopping chat HTTP server")
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
        except asyncio.IncompleteReadError:
            writer.close()
            await writer.wait_closed()
            return

        if not request_line:
            writer.close()
            await writer.wait_closed()
            return

        parts = request_line.decode("utf-8", errors="ignore").strip().split()
        if len(parts) < 2:
            writer.close()
            await writer.wait_closed()
            return

        method, path = parts[0], parts[1]

        # Consume and discard remaining headers
        while True:
            line = await reader.readline()
            if not line or line in {b"\r\n", b"\n"}:
                break

        if method != "GET" or path != "/":
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(self._html)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + self._html
        )

        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()


_CHAT_SERVER: Optional[_ChatWebSocketServer] = None
_HTTP_SERVER: Optional[_StaticHTTPServer] = None


async def on_startup(context: PluginContext) -> None:
    global _CHAT_SERVER, _HTTP_SERVER

    config = _load_config(context.config_dir / _CONFIG_FILENAME)

    if websockets is None:
        LOGGER.warning(
            "chat_server plugin disabled because the 'websockets' dependency is missing"
        )
        return

    ws_host = config.get("ws_host", _DEFAULT_CONFIG["ws_host"])
    ws_port = int(config.get("ws_port", _DEFAULT_CONFIG["ws_port"]))
    http_host = config.get("http_host", _DEFAULT_CONFIG["http_host"])
    http_port = int(config.get("http_port", _DEFAULT_CONFIG["http_port"]))
    room_name = config.get("room_name", _DEFAULT_CONFIG["room_name"])

    token = config.get("token")

    _CHAT_SERVER = _ChatWebSocketServer(ws_host, ws_port, room_name, token=token)
    await _CHAT_SERVER.start()

    html = _HTML_TEMPLATE.format(
        ws_url=_build_ws_url(ws_host, ws_port, token),
        room_name=room_name,
    )
    _HTTP_SERVER = _StaticHTTPServer(http_host, http_port, html)
    await _HTTP_SERVER.start()


async def on_event(event_name: str, payload: Dict[str, Any]) -> None:
    if _CHAT_SERVER is None:
        LOGGER.debug("Chat server not running; ignoring event %s", event_name)
        return

    if event_name == "transaction_received":
        tip_message = _format_tip_message(payload)
        await _CHAT_SERVER.system_message(tip_message)
        await _CHAT_SERVER.broadcast(
            {
                "type": "tip",
                "data": {
                    "amount_sats": payload.get("amount_sats"),
                    "memo": payload.get("memo"),
                    "txid": payload.get("txid", "")[-8:] or "unknown",
                },
            }
        )
    elif event_name == "automation.action":
        await _handle_automation_action(payload)


async def on_shutdown(context: PluginContext) -> None:
    global _CHAT_SERVER, _HTTP_SERVER

    if _CHAT_SERVER is not None:
        await _CHAT_SERVER.stop()
        _CHAT_SERVER = None

    if _HTTP_SERVER is not None:
        await _HTTP_SERVER.stop()
        _HTTP_SERVER = None


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(_DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(_DEFAULT_CONFIG)

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOGGER.warning("Invalid chat server config at %s; using defaults", path)
        return dict(_DEFAULT_CONFIG)

    merged = dict(_DEFAULT_CONFIG)
    merged.update({k: v for k, v in loaded.items() if k in _DEFAULT_CONFIG})
    return merged


def _format_tip_message(payload: Dict[str, Any]) -> str:
    amount = payload.get("amount_sats")
    txid = payload.get("txid", "")[-8:] or "unknown"
    memo = payload.get("memo")
    base = f"⚡ {amount} sats tip received (tx {txid})" if amount is not None else "⚡ New tip received"
    if memo:
        base += f" — {memo}"
    return base


async def _handle_automation_action(payload: Dict[str, Any]) -> None:
    if _CHAT_SERVER is None:
        return

    action = str(payload.get("action", "")).lower()
    if action in {"chat.message", "chat_message"}:
        message = str(payload.get("message", ""))
        if not message:
            return
        user = payload.get("user") or "automation"
        await _CHAT_SERVER.broadcast(
            {"type": "chat", "user": user, "message": message}
        )
    elif action in {"chat.system", "system_message"}:
        message = str(payload.get("message", ""))
        if not message:
            return
        await _CHAT_SERVER.system_message(message)


def _build_ws_url(host: str, port: int, token: Optional[str]) -> str:
    base = f"ws://{host}:{port}"
    if token:
        return f"{base}/?token={token}"
    return base


def _extract_params(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}

    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return {key: values[0] for key, values in params.items() if values}


async def _send_error(
    websocket: WebSocketServerProtocol,
    code: str,
    message: str,
) -> None:
    payload = {"type": "error", "error": {"code": code, "message": message}}
    try:
        await websocket.send(json.dumps(payload))
    except Exception:
        LOGGER.debug("Failed to send chat error payload", exc_info=True)

