"""Canonical payload schemas emitted by the wallet adapter.

The project primarily exchanges JSON payloads across process boundaries, but
having typed helpers for the most common wallet events makes it easier to keep
plugins, tests and remote clients in sync.  The dataclasses defined here expose
``to_payload`` helpers that collapse into ``dict`` objects ready for
serialisation while ensuring fields are consistently shaped.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass(slots=True)
class TransactionReceived:
    """Structured representation of an inbound on-chain transaction."""

    wallet: str
    txid: str
    amount_sats: int
    label: Optional[str] = None
    status: Optional[str] = None
    confirmations: int = 0
    memo: Optional[str] = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        # Normalise optional containers so downstream JSON encoders behave.
        payload["tags"] = list(self.tags)
        return payload


@dataclass(slots=True)
class BalanceBreakdown:
    confirmed: int
    unconfirmed: int
    unmatured: int
    frozen: Optional[int] = None
    lightning: Optional[str] = None
    lightning_frozen: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value is not None}


@dataclass(slots=True)
class BalanceUpdated:
    wallet: str
    balances: BalanceBreakdown

    def to_payload(self) -> Dict[str, Any]:
        return {"wallet": self.wallet, "balances": self.balances.to_payload()}


@dataclass(slots=True)
class ConnectionState:
    state: str
    server: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        payload = {"state": self.state}
        if self.server:
            payload["server"] = self.server
        return payload


@dataclass(slots=True)
class ServiceHealth:
    """Structured payload representing a service health snapshot."""

    component: str
    state: str
    severity: str
    server: Optional[str] = None
    last_change: Optional[str] = None
    details: Optional[Mapping[str, Any]] = None
    metadata: Optional[Mapping[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "component": self.component,
            "state": self.state,
            "severity": self.severity,
        }

        if self.server is not None:
            payload["server"] = self.server

        if self.last_change is not None:
            payload["last_change"] = self.last_change

        if self.details is not None:
            payload["details"] = dict(self.details)

        return merge_metadata(payload, self.metadata)


def merge_metadata(payload: Dict[str, Any], metadata: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return a new payload dict merged with optional metadata."""

    if not metadata:
        return payload

    merged = dict(payload)
    merged.update(metadata)
    return merged
