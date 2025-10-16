"""Automation plugin runtime.

The real project will provide a rich rule system that allows creators to wire
incoming wallet activity to bespoke actions.  For the purposes of the test
suite we expose a tiny but functional engine that watches Electrum events and
emits ``automation.action`` payloads derived from a rules file.

The behaviour implemented here focuses on a couple of developer conveniences:

* Rules are stored as JSON next to the plugin configuration directory.  A
  sensible set of defaults is created on first start so tests have predictable
  behaviour.
* Rules can be reloaded at runtime via :meth:`AutomationEngine.reload_rules` so
  hot-reload functionality can be exercised by the plugin manager.
* Incoming ``transaction_received`` events run through the rule matcher and
  actions are emitted through the shared :class:`~electrum_command_center.core.event_dispatcher.EventDispatcher`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ...core.plugin_context import PluginContext


LOGGER = logging.getLogger(__name__)

PLUGIN_NAME = "automation"
EVENT_SUBSCRIPTIONS = ["transaction_received", "transaction_confirmed"]


DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "name": "celebrate_inbound_transaction",
        "event": "transaction_received",
        "conditions": {"min_amount_sats": 1},
        "actions": [
            {
                "type": "play_sound",
                "sound": "coin_received",
            },
            {
                "type": "chat.message",
                "message": "New payment received!",
            },
        ],
    }
]


ENGINE: AutomationEngine | None = None


@dataclass
class AutomationEngine:
    """Lightweight rule engine used by the tests."""

    context: PluginContext
    rules_path: Path = field(init=False)
    _rules: List[Dict[str, Any]] = field(default_factory=list, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.rules_path = self.context.config_dir / "automation_rules.json"
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    async def startup(self) -> None:
        await self._ensure_rules_file()
        await self.reload_rules()

    async def shutdown(self) -> None:
        await self.context.cancel_tasks()

    async def reload_rules(self) -> None:
        async with self._lock:
            try:
                payload = self.rules_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                await self._write_rules(DEFAULT_RULES)
                payload = json.dumps(DEFAULT_RULES)

            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid automation rules file at {self.rules_path}: {exc}"
                ) from exc

            if not isinstance(data, list):
                raise RuntimeError(
                    f"Automation rules at {self.rules_path} must be a list"
                )

            self._rules = [
                rule for rule in data if isinstance(rule, dict) and rule.get("event")
            ]

    async def handle_event(self, event_name: str, payload: Dict[str, Any]) -> None:
        if event_name != "transaction_received":
            return

        matching_rules = [
            rule for rule in await self._get_rules_snapshot() if rule["event"] == event_name
        ]
        if not matching_rules:
            return

        amount = payload.get("amount_sats")
        for rule in matching_rules:
            if not self._matches(rule.get("conditions"), amount):
                continue

            for action in self._iter_actions(rule):
                await self.context.emit_event(
                    "automation.action",
                    {
                        "rule": rule.get("name", "unnamed"),
                        "action": action["type"],
                        "params": {
                            key: value for key, value in action.items() if key != "type"
                        },
                        "event": event_name,
                        "event_payload": dict(payload),
                    },
                )

    async def _ensure_rules_file(self) -> None:
        if self.rules_path.exists():
            return
        await self._write_rules(DEFAULT_RULES)

    async def _write_rules(self, rules: Iterable[Mapping[str, Any]]) -> None:
        serialisable = [dict(rule) for rule in rules]
        self.rules_path.write_text(
            json.dumps(serialisable, indent=2), encoding="utf-8"
        )

    async def _get_rules_snapshot(self) -> List[Dict[str, Any]]:
        async with self._lock:
            return [dict(rule) for rule in self._rules]

    @staticmethod
    def _matches(conditions: Optional[Mapping[str, Any]], amount: Any) -> bool:
        if not conditions:
            return True

        try:
            min_amount = conditions.get("min_amount_sats")
        except AttributeError:
            min_amount = None

        if min_amount is not None:
            try:
                if amount is None or int(amount) < int(min_amount):
                    return False
            except (TypeError, ValueError):
                return False

        return True

    @staticmethod
    def _iter_actions(rule: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        actions = rule.get("actions", [])
        for action in actions:
            if isinstance(action, Mapping) and "type" in action:
                yield action


async def on_startup(context: PluginContext) -> None:
    global ENGINE

    ENGINE = AutomationEngine(context)
    await ENGINE.startup()


async def on_shutdown(context: PluginContext) -> None:
    global ENGINE

    if ENGINE is None:
        return

    await ENGINE.shutdown()
    ENGINE = None


async def on_event(event_name: str, payload: Dict[str, Any]) -> None:
    """Handle wallet events for the local automation engine."""

    engine = ENGINE
    if engine is None:
        LOGGER.debug("Automation engine has not been started yet")
        return

    await engine.handle_event(event_name, payload)
