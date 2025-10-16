"""Adapter around the private Electrum wallet implementation.

The module provides an asynchronous façade so the rest of the Command Center
can remain decoupled from the actual Electrum fork.  For now the adapter emits
mock events so the event dispatcher and plugin pipeline can be exercised during
early development.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional


LOGGER = logging.getLogger(__name__)

WalletEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


class WalletAdapter:
    """Async wrapper around the Electrum wallet."""

    def __init__(self) -> None:
        self._callback: Optional[WalletEventCallback] = None
        self._running = False

    async def start(self) -> None:
        """Start the adapter.

        In the production system this would ensure the Electrum wallet is fully
        initialised and connected.  The placeholder simply flips an internal
        flag.
        """

        self._running = True
        LOGGER.info("Wallet adapter started")

    async def stop(self) -> None:
        self._running = False
        LOGGER.info("Wallet adapter stopped")

    def register_event_callback(self, callback: WalletEventCallback) -> None:
        """Register the dispatcher callback."""

        self._callback = callback

    async def emit_event(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Emit an event to the dispatcher if the adapter is running."""

        if not self._running:
            LOGGER.warning("Attempted to emit '%s' while adapter is stopped", event_name)
            return

        if not self._callback:
            LOGGER.warning("No event callback registered for '%s'", event_name)
            return

        await self._callback(event_name, payload)

    # ------------------------------------------------------------------
    # Simulation helpers used for early development
    # ------------------------------------------------------------------
    async def simulate_transaction_received(self, **payload: Any) -> None:
        await self.emit_event("transaction_received", payload)

    async def simulate_balance_updated(self, **payload: Any) -> None:
        await self.emit_event("balance_updated", payload)

    async def simulate_transaction_confirmed(self, **payload: Any) -> None:
        await self.emit_event("transaction_confirmed", payload)

