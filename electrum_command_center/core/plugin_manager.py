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
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from .event_dispatcher import EventDispatcher
from .plugin_context import PluginContext


try:  # pragma: no cover - optional dependency for hot reloads
    from watchfiles import Change, awatch
except ImportError:  # pragma: no cover - optional dependency for hot reloads
    Change = None  # type: ignore[assignment]
    awatch = None  # type: ignore[assignment]


LOGGER = logging.getLogger(__name__)


EventPayload = Dict[str, Any]
PluginHandler = Callable[[str, EventPayload], Awaitable[None]]


@dataclass
class PluginMetadata:
    """Runtime metadata about a loaded plugin."""

    name: str
    module: ModuleType
    path: Path
    event_subscriptions: List[str]
    handler: PluginHandler
    tasks: List[asyncio.Task] = field(default_factory=list)
    queues: Dict[str, asyncio.Queue] = field(default_factory=dict)
    plugin_tasks: List[asyncio.Task] = field(default_factory=list)
    on_startup: Optional[Callable[[PluginContext], Awaitable[None]]] = None
    on_shutdown: Optional[Callable[[PluginContext], Awaitable[None]]] = None
    context: Optional[PluginContext] = None
    status: str = "stopped"
    last_error: Optional[str] = None
    failure_count: int = 0


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
        queue_maxsize: Optional[int] = None,
        state_root: Optional[Path] = None,
    ) -> None:
        self._plugins_root = plugins_root
        self._event_dispatcher = event_dispatcher
        self._dev_mode = dev_mode
        self._loop = loop or asyncio.get_event_loop()
        self._plugins: Dict[str, PluginMetadata] = {}
        self._watcher_task: Optional[asyncio.Task] = None
        self._queue_maxsize = queue_maxsize
        self._state_root = state_root or (Path.home() / ".electrum_command_center")
        self._config_root = self._state_root / "config"
        self._data_root = self._state_root / "plugins"

        for path in (self._state_root, self._config_root, self._data_root):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Plugin discovery & loading
    # ------------------------------------------------------------------
    async def load_plugins(self) -> None:
        """Load every plugin found in :attr:`plugins_root`."""

        LOGGER.info("Loading plugins from %s", self._plugins_root)
        for init_file in self._discover_plugin_files():
            await self.load_plugin(init_file)

    async def load_plugin_by_name(self, plugin_name: str) -> None:
        """Load a plugin using its directory name."""

        init_file = self._plugins_root / plugin_name / "init.py"
        if not init_file.is_file():
            raise PluginLoadError(
                f"Plugin '{plugin_name}' does not exist at {init_file}"
            )

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
        await self.load_plugin(metadata.path)

    async def unload_plugin(self, plugin_name: str) -> None:
        """Stop background tasks and remove a plugin."""

        metadata = self._plugins.pop(plugin_name, None)
        if not metadata:
            LOGGER.warning("Attempted to unload unknown plugin '%s'", plugin_name)
            return

        await self._teardown_metadata(metadata, cancel_context_tasks=False)

        try:
            if metadata.on_shutdown is not None and metadata.context is not None:
                await metadata.on_shutdown(metadata.context)
        except Exception:
            LOGGER.exception("Plugin '%s' failed during shutdown", metadata.name)

        if metadata.context is not None:
            await metadata.context.cancel_tasks()
            metadata.context = None

        if metadata.plugin_tasks:
            metadata.plugin_tasks.clear()

        if metadata.module.__name__ in sys.modules:
            del sys.modules[metadata.module.__name__]

    async def shutdown(self) -> None:
        """Terminate every plugin task."""

        await self.stop_watcher()
        await asyncio.gather(
            *(self.unload_plugin(name) for name in list(self._plugins.keys()))
        )

    # ------------------------------------------------------------------
    # Hot reload support
    # ------------------------------------------------------------------
    async def start_watcher(self) -> None:
        """Begin monitoring the plugin directory for changes in dev mode."""

        if not self._dev_mode:
            LOGGER.debug("Plugin watcher skipped (dev_mode disabled)")
            return

        if awatch is None:
            LOGGER.warning(
                "Hot reload disabled: install 'watchfiles' to enable plugin watching"
            )
            return

        if self._watcher_task and not self._watcher_task.done():
            return

        if not self._plugins_root.exists():
            LOGGER.warning(
                "Plugin watcher skipped because %s does not exist", self._plugins_root
            )
            return

        LOGGER.info("Starting plugin hot reload watcher for %s", self._plugins_root)
        self._watcher_task = self._loop.create_task(self._watch_plugins())

    async def stop_watcher(self) -> None:
        """Stop the background watcher task if it is running."""

        task = self._watcher_task
        if not task:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover - expected during shutdown
            pass
        finally:
            self._watcher_task = None

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------
    def list_available_plugins(self) -> List[str]:
        """Return plugin directories that expose an ``init.py`` file."""

        return [
            init_file.parent.name
            for init_file in self._discover_plugin_files()
        ]

    def plugin_status(self) -> List[Dict[str, Any]]:
        """Return metadata about currently loaded plugins."""

        status: List[Dict[str, Any]] = []
        for metadata in sorted(self._plugins.values(), key=lambda item: item.name):
            status.append(
                {
                    "name": metadata.name,
                    "subscriptions": list(metadata.event_subscriptions),
                    "module": metadata.module.__name__,
                    "path": str(metadata.path),
                    "status": metadata.status,
                    "last_error": metadata.last_error,
                    "failures": metadata.failure_count,
                }
            )
        return status

    def dispatcher_metrics(self) -> Dict[str, Any]:
        """Expose dispatcher-level metrics and queue occupancy."""

        return {
            "events": self._event_dispatcher.metrics_snapshot(),
            "subscribers": self._event_dispatcher.subscriber_snapshot(),
        }

    def plugin_health(self) -> Dict[str, Any]:
        """Return lifecycle information and task state for loaded plugins."""

        plugins: List[Dict[str, Any]] = []
        for metadata in sorted(self._plugins.values(), key=lambda item: item.name):
            task_states: List[Dict[str, Any]] = []
            for task in metadata.plugin_tasks:
                state: Dict[str, Any] = {
                    "name": task.get_name(),
                    "done": task.done(),
                    "cancelled": task.cancelled(),
                }
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc:
                        state["exception"] = repr(exc)
                task_states.append(state)

            queue_states: List[Dict[str, Any]] = []
            for event_name, queue in metadata.queues.items():
                queue_states.append(
                    {
                        "event": event_name,
                        "size": queue.qsize(),
                        "maxsize": queue.maxsize,
                    }
                )

            plugins.append(
                {
                    "name": metadata.name,
                    "status": metadata.status,
                    "failures": metadata.failure_count,
                    "last_error": metadata.last_error,
                    "tasks": task_states,
                    "queues": queue_states,
                }
            )

        return {
            "plugins": plugins,
            "loaded": len(self._plugins),
        }

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

        on_startup = getattr(module, "on_startup", None)
        if on_startup is not None and not asyncio.iscoroutinefunction(on_startup):
            raise PluginLoadError(
                f"Plugin '{name}' defines 'on_startup' but it is not async"
            )

        on_shutdown = getattr(module, "on_shutdown", None)
        if on_shutdown is not None and not asyncio.iscoroutinefunction(on_shutdown):
            raise PluginLoadError(
                f"Plugin '{name}' defines 'on_shutdown' but it is not async"
            )

        return PluginMetadata(
            name=name,
            module=module,
            path=init_file.resolve(),
            event_subscriptions=subscriptions,
            handler=handler,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
        )

    async def _register_plugin(self, metadata: PluginMetadata) -> None:
        if metadata.name in self._plugins:
            raise PluginLoadError(
                f"A plugin named '{metadata.name}' is already registered"
            )

        for event_name in metadata.event_subscriptions:
            queue = self._event_dispatcher.register_queue(
                event_name,
                maxsize=self._queue_maxsize,
                subscriber_id=f"{metadata.name}:{event_name}",
            )
            task = self._loop.create_task(
                self._event_worker(metadata, event_name, queue)
            )
            metadata.tasks.append(task)
            metadata.queues[event_name] = queue

        metadata.context = self._build_context(metadata)
        metadata.status = "starting"
        metadata.last_error = None

        try:
            if metadata.on_startup is not None:
                await metadata.on_startup(metadata.context)
        except Exception:
            metadata.status = "error"
            await self._teardown_metadata(metadata)
            raise

        self._plugins[metadata.name] = metadata
        metadata.status = "running"

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

    async def _dispatch_event(
        self, metadata: PluginMetadata, event_name: str, event: Dict[str, Any]
    ) -> None:
        try:
            await metadata.handler(event_name, event["data"])
        except Exception as exc:  # pragma: no cover - defensive logging
            metadata.failure_count += 1
            metadata.last_error = str(exc)
            metadata.status = "error"
            LOGGER.exception("Plugin '%s' failed to handle event", metadata.name)
        else:
            if metadata.status != "running":
                metadata.status = "running"

    async def _watch_plugins(self) -> None:
        assert awatch is not None  # for type checkers

        try:
            async for changes in awatch(self._plugins_root):
                await self._handle_watcher_changes(changes)
        except asyncio.CancelledError:
            LOGGER.debug("Plugin watcher cancelled")
            raise
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.exception("Plugin watcher crashed")

    async def _handle_watcher_changes(
        self, changes: "Iterable[tuple[Change, str]]"
    ) -> None:
        if Change is None:  # pragma: no cover - defensive guard
            return

        to_reload: List[str] = []
        to_load: List[Path] = []
        to_unload: List[str] = []

        for change, path_str in changes:
            init_path = Path(path_str)
            if init_path.name != "init.py":
                continue

            plugin_name = init_path.parent.name

            if change == Change.added:
                to_load.append(init_path)
            elif change == Change.modified:
                to_reload.append(plugin_name)
            elif change == Change.deleted:
                to_unload.append(plugin_name)

        if not (to_reload or to_load or to_unload):
            return

        LOGGER.debug(
            "Plugin watcher detected changes: reload=%s load=%s unload=%s",
            to_reload,
            [str(path) for path in to_load],
            to_unload,
        )

        for plugin_name in to_unload:
            await self.unload_plugin(plugin_name)

        for init_path in to_load:
            await self.load_plugin(init_path)

        for plugin_name in to_reload:
            if plugin_name in self._plugins:
                await self.reload_plugin(plugin_name)
            else:
                init_path = self._plugins_root / plugin_name / "init.py"
                if init_path.exists():
                    await self.load_plugin(init_path)

    def _build_context(self, metadata: PluginMetadata) -> PluginContext:
        data_dir = (self._data_root / metadata.name).resolve()
        config_dir = (self._config_root / metadata.name).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)

        return PluginContext(
            name=metadata.name,
            dispatcher=self._event_dispatcher,
            loop=self._loop,
            data_dir=data_dir,
            config_dir=config_dir,
            _task_registry=metadata.plugin_tasks,
        )

    async def _teardown_metadata(
        self, metadata: PluginMetadata, *, cancel_context_tasks: bool = True
    ) -> None:
        for queue in metadata.queues.values():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

        if metadata.tasks:
            results = await asyncio.gather(
                *metadata.tasks, return_exceptions=True
            )
            for result in results:
                if isinstance(result, Exception) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    LOGGER.warning(
                        "Background task for plugin '%s' exited with %s",
                        metadata.name,
                        result,
                    )

        for event_name, queue in metadata.queues.items():
            await self._event_dispatcher.unregister_queue(event_name, queue)

        metadata.queues.clear()
        metadata.tasks.clear()

        if cancel_context_tasks and metadata.context is not None:
            await metadata.context.cancel_tasks()
            metadata.context = None

        if cancel_context_tasks and metadata.plugin_tasks:
            metadata.plugin_tasks.clear()

        metadata.status = "stopped"

