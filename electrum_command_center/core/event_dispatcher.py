"""Async event dispatcher bridging the wallet adapter and plugins."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, DefaultDict, Dict, List, Optional


LOGGER = logging.getLogger(__name__)

WalletEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


@dataclass
class _Subscription:
    """Internal bookkeeping for a subscriber queue."""

    event: str
    queue: "asyncio.Queue[Dict[str, Any]]"
    subscriber_id: str


class EventDispatcher:
    """Fan out wallet events to interested consumers via asyncio queues."""

    def __init__(
        self,
        *,
        default_queue_size: int = 0,
        overflow_strategy: str = "drop_new",
    ) -> None:
        if default_queue_size < 0:
            raise ValueError("default_queue_size must be >= 0")
        if overflow_strategy not in {"drop_new", "drop_oldest"}:
            raise ValueError(
                "overflow_strategy must be either 'drop_new' or 'drop_oldest'"
            )

        self._default_queue_size = default_queue_size
        self._overflow_strategy = overflow_strategy
        self._queues: DefaultDict[str, List[_Subscription]] = defaultdict(list)
        self._wildcard_key = "*"
        self._lock = asyncio.Lock()
        self._subscriber_seq = 0
        self._metrics_delivered: DefaultDict[str, int] = defaultdict(int)
        self._metrics_dropped: DefaultDict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    def register_queue(
        self,
        event_name: str,
        *,
        maxsize: Optional[int] = None,
        subscriber_id: Optional[str] = None,
    ) -> "asyncio.Queue[Dict[str, Any]]":
        """Register a new queue interested in ``event_name``.

        Consumers may pass ``"*"`` to receive every event.  Queues default to
        :attr:`default_queue_size` entries unless ``maxsize`` is provided.
        """

        if maxsize is None:
            maxsize = self._default_queue_size
        if maxsize < 0:
            raise ValueError("Queue maxsize must be >= 0")

        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=maxsize)
        if subscriber_id is None:
            subscriber_id = self._generate_subscriber_id(event_name)

        subscription = _Subscription(event=event_name, queue=queue, subscriber_id=subscriber_id)
        self._queues[event_name].append(subscription)
        return queue

    async def unregister_queue(self, event_name: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            subscriptions = self._queues.get(event_name)
            if not subscriptions:
                return

            for idx, subscription in enumerate(list(subscriptions)):
                if subscription.queue is queue:
                    subscriptions.pop(idx)
                    break
            if not subscriptions:
                self._queues.pop(event_name, None)

    def metrics_snapshot(self) -> Dict[str, Any]:
        """Return a snapshot of dispatcher activity counters."""

        subscribers: List[Dict[str, Any]] = []
        for event_name, subscriptions in self._queues.items():
            for subscription in subscriptions:
                subscribers.append(
                    {
                        "event": event_name,
                        "subscriber": subscription.subscriber_id,
                        "size": subscription.queue.qsize(),
                        "maxsize": subscription.queue.maxsize,
                    }
                )

        return {
            "delivered": dict(self._metrics_delivered),
            "dropped": dict(self._metrics_dropped),
            "subscribers": subscribers,
        }

    def reset_metrics(self) -> None:
        """Reset all cumulative metrics counters."""

        self._metrics_delivered.clear()
        self._metrics_dropped.clear()

    # ------------------------------------------------------------------
    # Wallet integration
    # ------------------------------------------------------------------
    def create_wallet_callback(self) -> WalletEventCallback:
        async def _callback(event_name: str, payload: Dict[str, Any]) -> None:
            await self.emit(event_name, payload)

        return _callback

    async def emit(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Push an event payload onto all subscriber queues."""

        subscriptions = list(self._queues.get(event_name, []))
        subscriptions.extend(self._queues.get(self._wildcard_key, []))

        if not subscriptions:
            LOGGER.debug("No subscribers for event '%s'", event_name)
            return

        message = {"event": event_name, "data": payload}
        for subscription in subscriptions:
            self._deliver(subscription, event_name, message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _deliver(
        self,
        subscription: _Subscription,
        event_name: str,
        message: Dict[str, Any],
    ) -> None:
        queue = subscription.queue
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            if self._overflow_strategy == "drop_new":
                self._metrics_dropped[event_name] += 1
                LOGGER.debug(
                    "Dropping new '%s' event for subscriber %s due to full queue",
                    event_name,
                    subscription.subscriber_id,
                )
                return

            if self._overflow_strategy == "drop_oldest":
                self._metrics_dropped[event_name] += 1
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    LOGGER.debug(
                        "Failed to drop oldest event for subscriber %s", subscription.subscriber_id
                    )
                    return
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    LOGGER.debug(
                        "Queue still full after dropping oldest event for subscriber %s",
                        subscription.subscriber_id,
                    )
                    return
                else:
                    self._metrics_delivered[event_name] += 1
                    return

            LOGGER.error("Unknown overflow strategy '%s'", self._overflow_strategy)
            return
        else:
            self._metrics_delivered[event_name] += 1

    def _generate_subscriber_id(self, event_name: str) -> str:
        self._subscriber_seq += 1
        return f"subscriber-{event_name}-{self._subscriber_seq}"

