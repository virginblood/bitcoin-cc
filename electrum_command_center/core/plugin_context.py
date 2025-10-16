"""Utilities exposed to plugins at runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional

from .event_dispatcher import EventDispatcher


@dataclass
class PluginContext:
    """Context object injected into plugins during startup.

    The context exposes shared services (event dispatcher, event loop) and
    convenience helpers so plugins can remain decoupled from the core
    implementation while still launching background tasks or emitting custom
    events back into the system.
    """

    name: str
    dispatcher: EventDispatcher
    loop: asyncio.AbstractEventLoop
    data_dir: Path
    config_dir: Path
    _task_registry: List[asyncio.Task] = field(default_factory=list)

    def create_task(
        self,
        coro: Awaitable[Any],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task:
        """Schedule ``coro`` on the shared loop and track it for teardown."""

        task = self.loop.create_task(coro, name=name)
        self._task_registry.append(task)
        return task

    async def emit_event(self, event_name: str, payload: Dict[str, Any]) -> None:
        """Re-emit an event through the central dispatcher."""

        await self.dispatcher.emit(event_name, payload)

    async def cancel_tasks(self) -> None:
        """Cancel tasks created via :meth:`create_task`."""

        if not self._task_registry:
            return

        for task in list(self._task_registry):
            if task.done():
                continue
            task.cancel()

        await asyncio.gather(*self._task_registry, return_exceptions=True)
        self._task_registry.clear()

