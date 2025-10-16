"""Fetch dispatcher metrics from a running Command Center instance."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

try:  # pragma: no cover - optional dependency guard
    import websockets
except ImportError:  # pragma: no cover - optional dependency guard
    websockets = None

LOGGER = logging.getLogger("electrum_command_center.cli.dispatcher")


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="WebSocket host")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port")
    parser.add_argument(
        "--token",
        default="dev-token",
        help="Authentication token matching the WebSocket server configuration",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Connection timeout in seconds",
    )
    parser.add_argument(
        "--action",
        default="health",
        choices=["health", "plugins"],
        help="Which management action to invoke",
    )
    return parser


async def _fetch_metrics(host: str, port: int, token: str, action: str, timeout: float) -> Any:
    if websockets is None:
        raise RuntimeError("The 'websockets' dependency is required to run this CLI")

    uri = f"ws://{host}:{port}/?token={token}"
    LOGGER.debug("Connecting to %s", uri)

    async with websockets.connect(uri, open_timeout=timeout) as websocket:
        if action == "plugins":
            await websocket.send(json.dumps({"action": "list_plugins"}))
        else:
            await websocket.send(json.dumps({"action": "health"}))

        response = await websocket.recv()
        try:
            return json.loads(response)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive logging
            raise RuntimeError(f"Server returned invalid JSON: {response!r}") from exc


def main() -> None:
    parser = _build_argument_parser()
    args = parser.parse_args()

    payload = asyncio.run(
        _fetch_metrics(
            args.host,
            args.port,
            args.token,
            args.action,
            args.timeout,
        )
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
