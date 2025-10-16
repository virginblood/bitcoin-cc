import asyncio
import json

from ..core.event_dispatcher import EventDispatcher
from ..core.plugin_context import PluginContext
from ..plugins.automation import init as automation


def test_automation_emits_actions_and_reloads(tmp_path) -> None:
    asyncio.run(_test_automation_emits_actions_and_reloads(tmp_path))


async def _test_automation_emits_actions_and_reloads(tmp_path) -> None:
    dispatcher = EventDispatcher()
    loop = asyncio.get_running_loop()
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    context = PluginContext(
        name="automation",
        dispatcher=dispatcher,
        loop=loop,
        data_dir=data_dir,
        config_dir=config_dir,
    )

    await automation.on_startup(context)

    queue = dispatcher.register_queue("automation.action", subscriber_id="test")

    await automation.on_event(
        "transaction_received",
        {"amount_sats": 2_500, "txid": "abc123", "memo": "cheers"},
    )

    first = await asyncio.wait_for(queue.get(), timeout=1)
    second = await asyncio.wait_for(queue.get(), timeout=1)
    actions = {first["data"]["action"], second["data"]["action"]}
    assert {"play_sound", "chat.message"} <= actions

    rules_path = config_dir / "automation_rules.json"
    rules = json.loads(rules_path.read_text())
    rules.append(
        {
            "name": "system_notice",
            "event": "transaction_received",
            "conditions": {"min_amount_sats": 10},
            "actions": [
                {"type": "chat.system", "message": "Whale detected"},
            ],
        }
    )
    rules_path.write_text(json.dumps(rules, indent=2), encoding="utf-8")

    assert automation.ENGINE is not None
    await automation.ENGINE.reload_rules()
    assert automation.ENGINE.rule_count == len(rules)

    await automation.on_shutdown(context)
