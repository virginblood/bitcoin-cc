"""Async event dispatcher bridging the wallet adapter and plugins."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, DefaultDict, Dict, List, Optional


LOGGER = logging.getLogger(__name__)

WalletEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


@dataclass
class SubscriberQueue:
    event: str
    subscriber_id: str
    queue: "asyncio.Queue[Dict[str, Any]]"


class EventDispatcher:
    """Fan out wallet events to interested consumers via asyncio queues."""

    _VALID_OVERFLOW_STRATEGIES = {"drop_new", "drop_oldest", "block"}

    def __init__(
        self,
        *,
        default_queue_size: int = 0,
        overflow_strategy: str = "drop_new",
    ) -> None:
        if overflow_strategy not in self._VALID_OVERFLOW_STRATEGIES:
            raise ValueError(
                "overflow_strategy must be one of "
                f"{sorted(self._VALID_OVERFLOW_STRATEGIES)}"
            )

        if default_queue_size < 0:
            raise ValueError("default_queue_size must be >= 0")

        self._queues: DefaultDict[str, List[SubscriberQueue]] = defaultdict(list)
        self._wildcard_key = "*"
        self._lock = asyncio.Lock()
        self._default_queue_size = default_queue_size
        self._overflow_strategy = overflow_strategy
        self._delivered_counts: Counter[str] = Counter()
        self._dropped_counts: Counter[str] = Counter()
        self._queue_index: Dict[asyncio.Queue, SubscriberQueue] = {}

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    def register_queue(
        self,
        event_name: str,
        *,
        maxsize: Optional[int] = None,
        subscriber_id: str = "anonymous",
    ) -> "asyncio.Queue[Dict[str, Any]]":
        """Register a new queue interested in ``event_name``.

        Consumers may pass ``"*"`` to receive every event.
        """

        size = self._default_queue_size if maxsize is None else max(maxsize, 0)
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=size)
        metadata = SubscriberQueue(
            event=event_name, subscriber_id=subscriber_id, queue=queue
        )
        self._queues[event_name].append(metadata)
        self._queue_index[queue] = metadata
        return queue

    async def unregister_queue(self, event_name: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            queues = self._queues.get(event_name)
            if not queues:
                return
            for metadata in list(queues):
                if metadata.queue is queue:
                    queues.remove(metadata)
                    break
            if not queues and event_name in self._queues:
                del self._queues[event_name]
            self._queue_index.pop(queue, None)

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

        message = {"event": event_name, "data": payload}
        for subscriber in queues:
            await self._dispatch_to_queue(subscriber, event_name, message)

    async def _dispatch_to_queue(
        self,
        subscriber: SubscriberQueue,
        event_name: str,
        message: Dict[str, Any],
    ) -> None:
        queue = subscriber.queue
        if queue.maxsize == 0:
            await queue.put(message)
            self._delivered_counts[event_name] += 1
            return

        if self._overflow_strategy == "block":
            await queue.put(message)
            self._delivered_counts[event_name] += 1
            return

        while True:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._dropped_counts[event_name] += 1
                queue_size = queue.qsize()
                LOGGER.warning(
                    "Queue overflow for event '%s' (subscriber=%s strategy=%s size=%d)",
                    event_name,
                    subscriber.subscriber_id,
                    self._overflow_strategy,
                    queue.maxsize,
                    extra={
                        "event_name": event_name,
                        "subscriber": subscriber.subscriber_id,
                        "strategy": self._overflow_strategy,
                        "queue_size": queue_size,
                    },
                )

                if self._overflow_strategy == "drop_new":
                    return

                if self._overflow_strategy == "drop_oldest":
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0)
                        continue
                    else:
                        continue

                return
            else:
                self._delivered_counts[event_name] += 1
                return

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def metrics_snapshot(self) -> Dict[str, Dict[str, int]]:
        """Return a snapshot of delivered and dropped event counters."""

        return {
            "delivered": dict(self._delivered_counts),
            "dropped": dict(self._dropped_counts),
        }

    def subscriber_snapshot(self) -> List[Dict[str, Any]]:
        """Return current queue occupancy for every subscriber."""

        snapshot: List[Dict[str, Any]] = []
        for metadata in self._queue_index.values():
            queue = metadata.queue
            snapshot.append(
                {
                    "event": metadata.event,
                    "subscriber": metadata.subscriber_id,
                    "size": queue.qsize(),
                    "maxsize": queue.maxsize,
                }
            )
        return snapshot

    def reset_metrics(self) -> None:
        """Reset the internal event delivery counters."""

        self._delivered_counts.clear()
        self._dropped_counts.clear()

