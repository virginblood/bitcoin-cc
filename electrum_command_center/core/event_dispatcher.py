"""Async event dispatcher bridging the wallet adapter and plugins."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, DefaultDict, Dict, List


LOGGER = logging.getLogger(__name__)

WalletEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class EventDispatcher:
    """Fan out wallet events to interested consumers via asyncio queues."""

    def __init__(self) -> None:
        self._queues: DefaultDict[str, List[asyncio.Queue]] = defaultdict(list)
        self._wildcard_key = "*"
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    def register_queue(self, event_name: str) -> "asyncio.Queue[Dict[str, Any]]":
        """Register a new queue interested in ``event_name``.

        Consumers may pass ``"*"`` to receive every event.
        """

        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self._queues[event_name].append(queue)
        return queue

    async def unregister_queue(self, event_name: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            queues = self._queues.get(event_name)
            if not queues:
                return
            try:
                queues.remove(queue)
            except ValueError:
                return

    # ------------------------------------------------------------------
    # Wallet integration
    # ------------------------------------------------------------------
    def create_wallet_callback(self) -> WalletEventCallback:
        async def _callback(event_name: str, payload: Dict[str, Any]) -> None:
            await self.emit(event_name, payload)

        return _callback

    async def emit(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Push an event payload onto all subscriber queues."""

        queues = list(self._queues.get(event_name, []))
        queues.extend(self._queues.get(self._wildcard_key, []))

        if not queues:
            LOGGER.debug("No subscribers for event '%s'", event_name)
            return

        for queue in queues:
            await queue.put({"event": event_name, "data": payload})

