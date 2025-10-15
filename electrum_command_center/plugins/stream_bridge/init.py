"""Stream bridge plugin.

This built-in plugin demonstrates how external presentation layers such as
streaming overlays can react to wallet activity.  The implementation is
deliberately lightweight and only logs activity for now, but the structure
mirrors how a websocket broadcaster could be wired in future iterations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict


LOGGER = logging.getLogger(__name__)

PLUGIN_NAME = "stream_bridge"
EVENT_SUBSCRIPTIONS = ["transaction_received"]


async def on_event(event_name: str, payload: Dict[str, Any]) -> None:
    """Handle wallet events for streaming integrations.

    The coroutine simply logs the received payload.  In the real project this
    is the place to forward structured websocket messages to overlay clients or
    trigger callbacks in broadcasting software such as OBS.
    """

    await asyncio.sleep(0)  # make the coroutine yield for consistency
    LOGGER.info("[Stream Bridge] Event %s received: %s", event_name, payload)
