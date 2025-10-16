"""Adapter around the Electrum wallet implementation.

The adapter keeps the rest of the Command Center insulated from Electrum's
internals while still giving us a reliable bridge for real wallet events.

In development environments a wallet instance might not be available yet.  The
adapter therefore supports two modes of operation:

* **Integrated mode** – When an Electrum wallet/network instance is supplied we
  register callbacks with Electrum's :mod:`electrum.util` callback manager and
  convert raw Electrum events (``new_transaction``, ``wallet_updated``) into the
  Command Center's canonical event schema.
* **Simulation mode** – When Electrum is absent we fall back to the historical
  behaviour where helper methods such as :meth:`simulate_transaction_received`
  can be used to emit synthetic events for testing the plugin pipeline.

Both modes are async-friendly and share the same :class:`WalletEventCallback`
interface so downstream consumers never need to know which mode is currently in
use.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple


try:  # pragma: no cover - optional during early development
    from electrum.network import Network
    from electrum.transaction import Transaction
    from electrum.util import register_callback, unregister_callback
    from electrum.wallet import Abstract_Wallet as Wallet
except Exception:  # pragma: no cover - Electrum might be unavailable on CI
    Network = "Network"  # type: ignore[misc,assignment]
    Transaction = "Transaction"  # type: ignore[misc,assignment]
    Wallet = "Wallet"  # type: ignore[misc,assignment]
    register_callback = None  # type: ignore[assignment]
    unregister_callback = None  # type: ignore[assignment]


from .event_schemas import (
    BalanceBreakdown,
    BalanceUpdated,
    ConnectionState,
    TransactionReceived,
    merge_metadata,
)


LOGGER = logging.getLogger(__name__)

WalletEventCallback = Callable[[str, Dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class ElectrumEventSpec:
    """Definition describing how to translate an Electrum callback."""

    target_event: str
    builder: Callable[[Tuple[Any, ...]], Awaitable[Optional[Dict[str, Any]]] | Optional[Dict[str, Any]]]
    requires_wallet: bool = True


class WalletAdapter:
    """Async wrapper around the Electrum wallet."""

    def __init__(
        self,
        *,
        wallet: Optional["Wallet"] = None,
        network: Optional["Network"] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        electrum_events: Optional[Dict[str, ElectrumEventSpec]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._callback: Optional[WalletEventCallback] = None
        self._running = False
        self._wallet = wallet
        self._network = network
        self._loop = loop or asyncio.get_event_loop()
        self._electrum_events = electrum_events or self._build_default_event_map()
        self._registered_callbacks: Dict[str, Callable[..., Awaitable[None]]] = {}
        self._lock = asyncio.Lock()
        self._retry_handle: Optional[asyncio.TimerHandle] = None
        self._metadata: Mapping[str, Any] = metadata or {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the adapter and register Electrum callbacks if available."""

        async with self._lock:
            if self._running:
                LOGGER.debug("Wallet adapter already running")
                return

            self._running = True
            LOGGER.info("Wallet adapter started")

            if self._wallet is not None and register_callback is not None:
                await self._register_electrum_callbacks()
            elif register_callback is None:
                LOGGER.debug(
                    "Electrum callback manager unavailable – running in simulation mode"
                )
            else:
                LOGGER.debug(
                    "No wallet attached to adapter – waiting for set_wallet() before binding callbacks"
                )

    async def stop(self) -> None:
        async with self._lock:
            if not self._running:
                return

            self._running = False
            await self._unregister_electrum_callbacks()
            LOGGER.info("Wallet adapter stopped")

    # ------------------------------------------------------------------
    # Electrum integration helpers
    # ------------------------------------------------------------------
    def set_wallet(self, wallet: "Wallet") -> None:
        """Attach an Electrum wallet instance at runtime."""

        self._wallet = wallet
        if self._running and register_callback is not None:
            # refresh callback bindings so we pick up the newly attached wallet
            self._loop.create_task(self._register_electrum_callbacks(refresh=True))

    def set_network(self, network: "Network") -> None:
        """Attach an Electrum network instance for status reporting."""

        self._network = network

    def set_event_specs(
        self, electrum_events: Dict[str, ElectrumEventSpec]
    ) -> None:
        """Override the Electrum→Command Center event translation map."""

        self._electrum_events = dict(electrum_events)
        if self._running and register_callback is not None:
            self._loop.create_task(self._register_electrum_callbacks(refresh=True))

    def set_metadata(self, metadata: Mapping[str, Any]) -> None:
        """Attach optional metadata merged into emitted payloads."""

        self._metadata = metadata

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
    # Internals – Electrum callback translation
    # ------------------------------------------------------------------
    async def _register_electrum_callbacks(self, *, refresh: bool = False) -> None:
        if register_callback is None:
            return

        if self._wallet is None:
            LOGGER.debug("Skipping Electrum bindings because no wallet is attached")
            return

        if refresh:
            await self._unregister_electrum_callbacks()

        failures: Dict[str, Exception] = {}

        for event_name, spec in self._electrum_events.items():
            if event_name in self._registered_callbacks:
                continue

            async def _callback(*args: Any, _event=event_name) -> None:
                await self._handle_electrum_event(_event, args)

            try:
                register_callback(_callback, [event_name])
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.exception(
                    "Failed to register Electrum callback for '%s'", event_name
                )
                failures[event_name] = exc
                continue

            self._registered_callbacks[event_name] = _callback
            LOGGER.debug("Registered Electrum callback for '%s'", event_name)

        if failures:
            self._schedule_retry()

    async def _unregister_electrum_callbacks(self) -> None:
        if unregister_callback is None:
            self._registered_callbacks.clear()
            return

        for event_name, callback in list(self._registered_callbacks.items()):
            try:
                unregister_callback(callback)
            except Exception:  # pragma: no cover - defensive logging
                LOGGER.exception(
                    "Failed to unregister Electrum callback for '%s'", event_name
                )
            else:
                LOGGER.debug("Unregistered Electrum callback for '%s'", event_name)
            self._registered_callbacks.pop(event_name, None)

        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None

    async def _handle_electrum_event(
        self, event_name: str, args: Tuple[Any, ...]
    ) -> None:
        if not self._running or not self._callback:
            return

        spec = self._electrum_events.get(event_name)
        if spec is None:
            LOGGER.debug("No translation spec for Electrum event '%s'", event_name)
            return

        if spec.requires_wallet and not self._wallet:
            LOGGER.debug(
                "Received Electrum event '%s' but no wallet is attached", event_name
            )
            return

        try:
            payload_or_coro = spec.builder(args)
            if asyncio.iscoroutine(payload_or_coro):
                payload = await payload_or_coro  # type: ignore[assignment]
            else:
                payload = payload_or_coro  # type: ignore[assignment]
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.exception("Failed to translate Electrum event '%s'", event_name)
            return

        if payload is None:
            return

        await self.emit_event(spec.target_event, payload)

    def _schedule_retry(self, delay: float = 5.0) -> None:
        if self._retry_handle is not None and not self._retry_handle.cancelled():
            return

        def _retry() -> None:
            self._retry_handle = None
            if not self._running:
                return
            self._loop.create_task(self._register_electrum_callbacks(refresh=True))

        LOGGER.debug("Retrying Electrum callback registration in %.1fs", delay)
        self._retry_handle = self._loop.call_later(delay, _retry)

    # ------------------------------------------------------------------
    # Default event translation
    # ------------------------------------------------------------------
    def _build_default_event_map(self) -> Dict[str, ElectrumEventSpec]:
        return {
            "new_transaction": ElectrumEventSpec(
                target_event="transaction_received",
                builder=self._build_transaction_received,
            ),
            "wallet_updated": ElectrumEventSpec(
                target_event="balance_updated",
                builder=self._build_wallet_updated,
            ),
            "network_updated": ElectrumEventSpec(
                target_event="connection_state",
                builder=self._build_network_updated,
                requires_wallet=False,
            ),
        }

    def _build_transaction_received(
        self, args: Tuple[Any, ...]
    ) -> Optional[Dict[str, Any]]:
        if not args:
            return None

        wallet_obj = args[0]
        if self._wallet is not None and wallet_obj is not self._wallet:
            return None

        if len(args) < 2:
            LOGGER.debug("Transaction event missing transaction payload")
            return None

        tx: "Transaction" = args[1]
        wallet: "Wallet" = wallet_obj

        details = wallet.get_tx_info(tx)
        txid = details.txid or tx.txid() or "unknown"
        amount_sats = details.amount if details.amount is not None else 0
        confirmations = details.tx_mined_status.conf or 0

        memo = getattr(details, "label", None)
        try:
            tags = tuple(sorted(set(details.tags)))  # type: ignore[attr-defined]
        except Exception:
            tags = ()

        payload = TransactionReceived(
            wallet=wallet.basename(),
            txid=txid,
            amount_sats=amount_sats,
            label=details.label,
            status=details.status,
            confirmations=confirmations,
            memo=memo,
            tags=tags,
        ).to_payload()

        return merge_metadata(payload, self._metadata)

    def _build_wallet_updated(
        self, args: Tuple[Any, ...]
    ) -> Optional[Dict[str, Any]]:
        if not args:
            return None

        wallet_obj = args[0]
        if self._wallet is not None and wallet_obj is not self._wallet:
            return None

        wallet: "Wallet" = wallet_obj
        confirmed, unconfirmed, unmatured = wallet.get_balance()

        try:
            pie = wallet.get_balances_for_piechart()
        except Exception:
            pie = None

        breakdown = BalanceBreakdown(
            confirmed=confirmed,
            unconfirmed=unconfirmed,
            unmatured=unmatured,
            frozen=getattr(pie, "frozen", None) if pie else None,
            lightning=str(getattr(pie, "lightning", "")) or None if pie else None,
            lightning_frozen=str(getattr(pie, "lightning_frozen", "")) or None
            if pie
            else None,
        )

        payload = BalanceUpdated(wallet=wallet.basename(), balances=breakdown).to_payload()
        return merge_metadata(payload, self._metadata)

    def _build_network_updated(
        self, args: Tuple[Any, ...]
    ) -> Optional[Dict[str, Any]]:
        if self._network is None:
            return {"state": "unknown"}

        network: "Network" = self._network
        try:
            params = network.get_parameters()
            server = params.server.host if params and params.server else None
        except Exception:
            server = None

        try:
            connected = network.is_connected()
        except Exception:
            connected = False

        payload = ConnectionState(
            state="connected" if connected else "disconnected",
            server=server,
        ).to_payload()
        return merge_metadata(payload, self._metadata)

    # ------------------------------------------------------------------
    # Simulation helpers used for early development
    # ------------------------------------------------------------------
    async def simulate_transaction_received(self, **payload: Any) -> None:
        await self.emit_event("transaction_received", payload)

    async def simulate_balance_updated(self, **payload: Any) -> None:
        await self.emit_event("balance_updated", payload)

    async def simulate_transaction_confirmed(self, **payload: Any) -> None:
        await self.emit_event("transaction_confirmed", payload)

