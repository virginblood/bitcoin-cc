"""Runtime monitor that translates core service events into health snapshots."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .event_dispatcher import EventDispatcher
from .event_schemas import ServiceHealth
from .telemetry_store import TelemetryStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _ServiceState:
    state: str
    severity: str
    server: Optional[str]
    last_change: str
    details: Dict[str, Any]


class ServiceHealthMonitor:
    """Observe dispatcher events and derive service health summaries."""

    def __init__(
        self,
        dispatcher: EventDispatcher,
        *,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        queue_maxsize: int = 0,
        telemetry_store: Optional[TelemetryStore] = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._loop = loop or asyncio.get_event_loop()
        self._queue: Optional[asyncio.Queue[Dict[str, Any]]] = None
        self._queue_maxsize = queue_maxsize
        self._task: Optional[asyncio.Task] = None
        self._state: Dict[str, _ServiceState] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._telemetry_store = telemetry_store

    async def start(self) -> None:
        if self._running:
            return

        self._queue = self._dispatcher.register_queue(
            "connection_state",
            maxsize=self._queue_maxsize,
            subscriber_id="service-health-monitor",
        )
        self._task = self._loop.create_task(self._run())
        self._running = True
        await self._handle_connection_state({})

    async def stop(self) -> None:
        if not self._running:
            return

        self._running = False
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        if self._queue is not None:
            await self._dispatcher.unregister_queue("connection_state", self._queue)
            self._queue = None

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return the latest known service health states."""

        return {
            name: {
                "state": service.state,
                "severity": service.severity,
                "server": service.server,
                "last_change": service.last_change,
                "details": dict(service.details),
            }
            for name, service in self._state.items()
        }

    async def _run(self) -> None:
        assert self._queue is not None
        try:
            while True:
                try:
                    message = await self._queue.get()
                except asyncio.CancelledError:
                    break

                if message is None:
                    break

                await self._handle_connection_state(message["data"])
        finally:
            self._task = None

    async def _handle_connection_state(self, payload: Dict[str, Any]) -> None:
        state = payload.get("state", "unknown")
        server = payload.get("server")
        severity = "ok" if state == "connected" else "degraded"
        last_change = _now_iso()

        async with self._lock:
            previous = self._state.get("network")
            if (
                previous
                and previous.state == state
                and previous.server == server
            ):
                previous.details["last_update"] = last_change
                previous.last_change = last_change
                return

            details: Dict[str, Any] = {}
            if previous is not None:
                details["previous_state"] = previous.state
                if previous.server:
                    details["previous_server"] = previous.server

            service_state = _ServiceState(
                state=state,
                severity=severity,
                server=server,
                last_change=last_change,
                details=details,
            )
            self._state["network"] = service_state

        health_payload = ServiceHealth(
            component="network",
            state=state,
            severity=severity,
            server=server,
            last_change=last_change,
            details=details or None,
        ).to_payload()

        if self._telemetry_store is not None:
            self._telemetry_store.record_service_health(health_payload)

        await self._dispatcher.emit("service_health", health_payload)
