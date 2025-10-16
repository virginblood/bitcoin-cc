"""Chat server plugin scaffold.

The plugin currently acts as a placeholder for the future websocket chat
bridge.  It records wallet events so that the chat transport can be wired in
later without touching the plugin manager or dispatcher interfaces.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict


LOGGER = logging.getLogger(__name__)

PLUGIN_NAME = "chat_server"
EVENT_SUBSCRIPTIONS = ["transaction_received", "balance_updated"]


async def on_event(event_name: str, payload: Dict[str, Any]) -> None:
    """React to wallet events and prepare chat notifications."""

    await asyncio.sleep(0)
    LOGGER.info("[Chat Server] Event %s received: %s", event_name, payload)
