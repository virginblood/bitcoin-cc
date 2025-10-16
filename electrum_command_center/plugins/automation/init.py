"""Automation plugin scaffold.

The automation engine will eventually contain a flexible rule system for
creators.  The present module keeps the interface alive so that the plugin
manager and dispatcher can be exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict


LOGGER = logging.getLogger(__name__)

PLUGIN_NAME = "automation"
EVENT_SUBSCRIPTIONS = ["transaction_received", "transaction_confirmed"]


async def on_event(event_name: str, payload: Dict[str, Any]) -> None:
    """Handle wallet events for the local automation engine."""

    await asyncio.sleep(0)
    LOGGER.info("[Automation] Event %s received: %s", event_name, payload)
