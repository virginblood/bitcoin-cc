"""Plugin management utilities for the Electrum Command Center.

The manager dynamically discovers plugin folders, loads their ``init.py``
modules and forwards events emitted by the :class:`EventDispatcher`.  The
implementation focuses on extensibility: plugins can be reloaded at runtime and
all handlers are executed asynchronously so they never block wallet updates.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .event_dispatcher import EventDispatcher


LOGGER = logging.getLogger(__name__)


EventPayload = Dict[str, Any]
PluginHandler = Callable[[str, EventPayload], Awaitable[None]]


@dataclass
class PluginMetadata:
    """Runtime metadata about a loaded plugin."""

    name: str
    module: ModuleType
    event_subscriptions: List[str]
    handler: PluginHandler
    tasks: List[asyncio.Task] = field(default_factory=list)
    queues: Dict[str, asyncio.Queue] = field(default_factory=dict)


class PluginLoadError(RuntimeError):
    """Raised when a plugin fails to load."""


class PluginManager:
    """Discover, load and coordinate plugins."""

    def __init__(
        self,
        plugins_root: Path,
        event_dispatcher: EventDispatcher,
        *,
        dev_mode: bool = False,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._plugins_root = plugins_root
        self._event_dispatcher = event_dispatcher
        self._dev_mode = dev_mode
        self._loop = loop or asyncio.get_event_loop()
        self._plugins: Dict[str, PluginMetadata] = {}

    # ------------------------------------------------------------------
    # Plugin discovery & loading
    # ------------------------------------------------------------------
    async def load_plugins(self) -> None:
        """Load every plugin found in :attr:`plugins_root`."""

        LOGGER.info("Loading plugins from %s", self._plugins_root)
        for init_file in self._discover_plugin_files():
            await self.load_plugin(init_file)

    async def load_plugin(self, init_file: Path) -> None:
        """Load a single plugin defined by ``init.py``."""

        module = self._import_plugin(init_file)
        metadata = self._validate_plugin(module, init_file)
        await self._register_plugin(metadata)
        LOGGER.info("Plugin '%s' loaded", metadata.name)

    async def reload_plugin(self, plugin_name: str) -> None:
        """Reload a plugin that is already active."""

        metadata = self._plugins.get(plugin_name)
        if not metadata:
            raise PluginLoadError(f"Plugin '{plugin_name}' is not loaded")

        LOGGER.info("Reloading plugin '%s'", plugin_name)
        await self.unload_plugin(plugin_name)
        await self.load_plugin(Path(metadata.module.__file__ or ""))

    async def unload_plugin(self, plugin_name: str) -> None:
        """Stop background tasks and remove a plugin."""

        metadata = self._plugins.pop(plugin_name, None)
        if not metadata:
            LOGGER.warning("Attempted to unload unknown plugin '%s'", plugin_name)
            return

        await self._teardown_metadata(metadata)

        if metadata.module.__name__ in sys.modules:
            del sys.modules[metadata.module.__name__]

    async def shutdown(self) -> None:
        """Terminate every plugin task."""

        await asyncio.gather(
            *(self.unload_plugin(name) for name in list(self._plugins.keys()))
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _discover_plugin_files(self) -> List[Path]:
        files: List[Path] = []
        if not self._plugins_root.exists():
            LOGGER.warning("Plugin directory %s does not exist", self._plugins_root)
            return files

        for candidate in sorted(self._plugins_root.iterdir()):
            init_file = candidate / "init.py"
            if init_file.is_file():
                files.append(init_file)
        return files

    def _import_plugin(self, init_file: Path) -> ModuleType:
        module_name = self._build_module_name(init_file)
        if module_name in sys.modules and self._dev_mode:
            LOGGER.debug("Reloading module %s in dev mode", module_name)
            return importlib.reload(sys.modules[module_name])

        spec = importlib.util.spec_from_file_location(module_name, init_file)
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"Could not create spec for plugin at {init_file}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _build_module_name(self, init_file: Path) -> str:
        relative_path = init_file.relative_to(self._plugins_root)
        plugin_name = relative_path.parent.name
        suffix = "_dev" if self._dev_mode else ""
        return f"electrum_command_center.plugins.{plugin_name}{suffix}"

    def _validate_plugin(self, module: ModuleType, init_file: Path) -> PluginMetadata:
        try:
            name = getattr(module, "PLUGIN_NAME")
            subscriptions = list(getattr(module, "EVENT_SUBSCRIPTIONS"))
            handler = getattr(module, "on_event")
        except AttributeError as exc:
            raise PluginLoadError(
                f"Plugin at {init_file} is missing a required attribute: {exc}"
            ) from exc

        if not asyncio.iscoroutinefunction(handler):
            raise PluginLoadError(
                f"Plugin '{name}' must define an async 'on_event' coroutine"
            )

        return PluginMetadata(
            name=name,
            module=module,
            event_subscriptions=subscriptions,
            handler=handler,
        )

    async def _register_plugin(self, metadata: PluginMetadata) -> None:
        if metadata.name in self._plugins:
            raise PluginLoadError(
                f"A plugin named '{metadata.name}' is already registered"
            )

        for event_name in metadata.event_subscriptions:
            queue = self._event_dispatcher.register_queue(event_name)
            task = self._loop.create_task(
                self._event_worker(metadata, event_name, queue)
            )
            metadata.tasks.append(task)
            metadata.queues[event_name] = queue

        self._plugins[metadata.name] = metadata

    async def _event_worker(
        self,
        metadata: PluginMetadata,
        event_name: str,
        queue: "asyncio.Queue[Dict[str, Any]]",
    ) -> None:
        while True:
            try:
                event = await queue.get()
            except asyncio.CancelledError:
                break

            if event is None:  # sentinel for shutdown
                break

            await self._dispatch_event(metadata, event_name, event)

    async def _teardown_metadata(self, metadata: PluginMetadata) -> None:
        for queue in metadata.queues.values():
            await self._enqueue_shutdown(queue)

        if metadata.tasks:
            await asyncio.gather(*metadata.tasks, return_exceptions=True)

        for event_name, queue in metadata.queues.items():
            await self._event_dispatcher.unregister_queue(event_name, queue)

        metadata.tasks.clear()
        metadata.queues.clear()

    async def _enqueue_shutdown(self, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        while True:
            try:
                queue.put_nowait(None)
                break
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0)
                else:
                    continue

    async def _dispatch_event(
        self, metadata: PluginMetadata, event_name: str, event: Dict[str, Any]
    ) -> None:
        try:
            await metadata.handler(event_name, event["data"])
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.exception("Plugin '%s' failed to handle event", metadata.name)

