"""In-memory persistence for Command Center telemetry snapshots."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Mapping


__all__ = ["TelemetryStore", "TelemetryEntry"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class TelemetryEntry:
    """Single telemetry record tagged with a timestamp."""

    timestamp: str
    payload: Mapping[str, Any]

    def to_payload(self) -> Dict[str, Any]:
        return {"timestamp": self.timestamp, "payload": dict(self.payload)}


class TelemetryStore:
    """Persist rolling telemetry snapshots for later inspection."""

    def __init__(
        self,
        *,
        max_health_records: int = 200,
        max_service_records: int = 200,
    ) -> None:
        if max_health_records < 0:
            raise ValueError("max_health_records must be non-negative")
        if max_service_records < 0:
            raise ValueError("max_service_records must be non-negative")

        self._health_history: Deque[TelemetryEntry] = deque(maxlen=max_health_records)
        self._service_history: Deque[TelemetryEntry] = deque(maxlen=max_service_records)

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------
    def record_health_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Store a dispatcher/plugin health snapshot."""

        entry = TelemetryEntry(timestamp=_now_iso(), payload=deepcopy(dict(snapshot)))
        self._health_history.append(entry)

    def record_service_health(self, payload: Mapping[str, Any]) -> None:
        """Store a service health event emitted by :mod:`ServiceHealthMonitor`."""

        entry = TelemetryEntry(timestamp=_now_iso(), payload=deepcopy(dict(payload)))
        self._service_history.append(entry)

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------
    def health_history(self) -> List[Dict[str, Any]]:
        return self._serialise(self._health_history)

    def service_history(self) -> List[Dict[str, Any]]:
        return self._serialise(self._service_history)

    def history(self) -> Dict[str, List[Dict[str, Any]]]:
        return {
            "health": self.health_history(),
            "service_health": self.service_history(),
        }

    def clear(self) -> None:
        self._health_history.clear()
        self._service_history.clear()

    def _serialise(self, entries: Iterable[TelemetryEntry]) -> List[Dict[str, Any]]:
        return [entry.to_payload() for entry in entries]
