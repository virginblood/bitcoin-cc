import asyncio
import textwrap

from ..core.event_dispatcher import EventDispatcher
from ..core.plugin_manager import PluginManager


def test_plugin_manager_reports_health(tmp_path) -> None:
    asyncio.run(_test_plugin_manager_reports_health(tmp_path))


async def _test_plugin_manager_reports_health(tmp_path) -> None:
    loop = asyncio.get_running_loop()
    dispatcher = EventDispatcher()

    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    demo = plugin_root / "demo"
    demo.mkdir()
    init_file = demo / "init.py"
    init_file.write_text(
        textwrap.dedent(
            """
            PLUGIN_NAME = "demo"
            EVENT_SUBSCRIPTIONS = ["transaction_received"]
            EVENTS = []

            async def on_event(event_name, payload):
                EVENTS.append((event_name, payload))
            """
        ),
        encoding="utf-8",
    )

    manager = PluginManager(plugin_root, dispatcher, loop=loop)
    await manager.load_plugins()

    await dispatcher.emit("transaction_received", {"amount_sats": 42})
    await asyncio.sleep(0)

    module = __import__("electrum_command_center.plugins.demo", fromlist=["EVENTS"])
    assert module.EVENTS[0][0] == "transaction_received"
    assert module.EVENTS[0][1]["amount_sats"] == 42

    health = manager.plugin_health()
    assert health["loaded"] == 1
    assert health["plugins"][0]["name"] == "demo"

    await manager.shutdown()
