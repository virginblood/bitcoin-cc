"""Local automation engine implementing rule-driven reactions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:  # pragma: no cover - optional dependency guard
    import aiohttp
except ImportError:  # pragma: no cover - optional dependency guard
    aiohttp = None

from electrum_command_center.core.plugin_context import PluginContext


LOGGER = logging.getLogger(__name__)

PLUGIN_NAME = "automation"
EVENT_SUBSCRIPTIONS = [
    "transaction_received",
    "transaction_confirmed",
    "balance_updated",
]

_CONFIG_FILENAME = "automation_rules.json"
_DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "name": "celebrate_large_tip",
        "event": "transaction_received",
        "conditions": {"min_amount_sats": 1000},
        "actions": [
            {
                "type": "log",
                "level": "info",
                "message": "🎉 Large tip of {amount_sats} sats received (tx {txid})",
            },
            {
                "type": "emit_event",
                "event": "automation.action",
                "payload": {
                    "action": "play_sound",
                    "sound": "airhorn",
                    "amount_sats": "{amount_sats}",
                    "txid": "{txid}",
                },
            },
            {
                "type": "chat_message",
                "user": "CommandCenter",
                "message": "Thank you for the {amount_sats} sats tip!",
            },
        ],
    },
    {
        "name": "sponsor_overlay",
        "event": "transaction_received",
        "conditions": {"memo_contains": "#sponsor"},
        "actions": [
            {
                "type": "emit_event",
                "event": "automation.action",
                "payload": {
                    "action": "overlay",
                    "template": "sponsor",
                    "memo": "{memo}",
                    "amount_sats": "{amount_sats}",
                    "txid": "{txid}",
                },
            },
            {
                "type": "log",
                "level": "info",
                "message": "Sponsor trigger fired for memo {memo}",
            },
        ],
    },
]


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class AutomationRule:
    name: str
    event: str
    conditions: Dict[str, Any]
    actions: List[Dict[str, Any]]

    def matches(self, payload: Dict[str, Any]) -> bool:
        return _conditions_match(self.conditions, payload)

    async def execute(self, context: PluginContext, payload: Dict[str, Any]) -> None:
        for action in self.actions:
            try:
                await _execute_action(action, context, payload, rule_name=self.name)
            except Exception:
                LOGGER.exception("Automation action failed (rule=%s)", self.name)


class AutomationEngine:
    def __init__(
        self,
        context: PluginContext,
        rules_path: Path,
        rules: Iterable[AutomationRule],
        *,
        reload_interval: float = 2.0,
    ) -> None:
        self._context = context
        self._rules_path = rules_path
        self._rules = list(rules)
        self._reload_interval = reload_interval
        self._watch_task: Optional[asyncio.Task] = None
        self._last_mtime = self._get_mtime()

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    async def start(self) -> None:
        if self._watch_task is None:
            self._watch_task = self._context.create_task(
                self._watch_rules(), name=f"automation-watch:{self._context.name}"
            )

    async def handle_event(self, event_name: str, payload: Dict[str, Any]) -> None:
        matches = [
            rule
            for rule in self._rules
            if rule.event in {event_name, "*"} and rule.matches(payload)
        ]

        if not matches:
            LOGGER.debug("No automation rules matched event %s", event_name)
            return

        LOGGER.info(
            "Executing %d automation rule(s) for event %s",
            len(matches),
            event_name,
        )

        for rule in matches:
            await rule.execute(self._context, payload)

    async def shutdown(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None
        await self._context.cancel_tasks()

    async def reload_rules(self) -> None:
        rules = _load_rules(self._rules_path)
        parsed = _parse_rules(rules)
        self._rules = parsed
        LOGGER.info("Automation rules reloaded (%d rule(s))", len(parsed))

    def _get_mtime(self) -> float:
        try:
            return self._rules_path.stat().st_mtime
        except FileNotFoundError:
            return 0.0

    async def _watch_rules(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._reload_interval)
                mtime = self._get_mtime()
                if mtime and mtime > self._last_mtime:
                    self._last_mtime = mtime
                    await self.reload_rules()
        except asyncio.CancelledError:
            LOGGER.debug("Automation rule watcher cancelled")
            raise

        await self._context.cancel_tasks()


ENGINE: Optional[AutomationEngine] = None


async def on_startup(context: PluginContext) -> None:
    global ENGINE

    rules_path = context.config_dir / _CONFIG_FILENAME
    rules = _load_rules(rules_path)
    parsed = _parse_rules(rules)
    ENGINE = AutomationEngine(context, rules_path, parsed)
    await ENGINE.start()
    LOGGER.info("Automation engine loaded with %d rule(s)", ENGINE.rule_count)


async def on_event(event_name: str, payload: Dict[str, Any]) -> None:
    if ENGINE is None:
        LOGGER.debug("Automation engine inactive; event %s ignored", event_name)
        return

    await ENGINE.handle_event(event_name, payload)


async def on_shutdown(context: PluginContext) -> None:
    global ENGINE

    if ENGINE is not None:
        await ENGINE.shutdown()
        ENGINE = None


def _conditions_match(conditions: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    amount = payload.get("amount_sats")

    minimum = conditions.get("min_amount_sats")
    if minimum is not None and (amount is None or amount < minimum):
        return False

    maximum = conditions.get("max_amount_sats")
    if maximum is not None and (amount is None or amount > maximum):
        return False

    memo = str(payload.get("memo", ""))
    contains = conditions.get("memo_contains")
    if contains and contains.lower() not in memo.lower():
        return False

    memo_regex = conditions.get("memo_regex")
    if memo_regex and not re.search(memo_regex, memo):
        return False

    min_conf = conditions.get("min_confirmations")
    if min_conf is not None and payload.get("confirmations", 0) < int(min_conf):
        return False

    tags_required = conditions.get("tags_any")
    if tags_required:
        tags = set(payload.get("tags", []))
        if not tags.intersection(set(tags_required)):
            return False

    equals = conditions.get("fields_equal", {})
    for key, expected in equals.items():
        if payload.get(key) != expected:
            return False

    return True


async def _execute_action(
    action: Dict[str, Any],
    context: PluginContext,
    payload: Dict[str, Any],
    *,
    rule_name: str,
) -> None:
    action_type = action.get("type")
    if action_type == "log":
        await _action_log(action, payload, rule_name=rule_name)
    elif action_type == "emit_event":
        await _action_emit_event(action, context, payload)
    elif action_type in {"delay", "sleep"}:
        await _action_delay(action)
    elif action_type == "webhook":
        await _action_webhook(action, payload, rule_name=rule_name)
    elif action_type in {"chat_message", "chat.message"}:
        await _action_chat(action, context, payload)
    elif action_type in {"overlay", "overlay_trigger"}:
        await _action_overlay(action, context, payload)
    elif action_type in {"media", "media_trigger", "play_sound"}:
        await _action_media(action, context, payload)
    else:
        LOGGER.warning(
            "Unsupported automation action '%s' in rule '%s'", action_type, rule_name
        )


async def _action_log(action: Dict[str, Any], payload: Dict[str, Any], *, rule_name: str) -> None:
    level = str(action.get("level", "info")).lower()
    message = _render_template(action.get("message", ""), payload)

    log_method = getattr(LOGGER, level, LOGGER.info)
    log_method("[Automation:%s] %s", rule_name, message)


async def _action_emit_event(
    action: Dict[str, Any], context: PluginContext, payload: Dict[str, Any]
) -> None:
    event = action.get("event")
    if not event:
        LOGGER.warning("emit_event action missing event field")
        return

    raw_payload = action.get("payload", {})
    rendered_payload = _render_template(raw_payload, payload)
    await context.emit_event(str(event), rendered_payload)


async def _action_delay(action: Dict[str, Any]) -> None:
    seconds = float(action.get("seconds") or action.get("duration", 0))
    if seconds > 0:
        await asyncio.sleep(seconds)


async def _action_webhook(
    action: Dict[str, Any], payload: Dict[str, Any], *, rule_name: str
) -> None:
    if aiohttp is None:
        LOGGER.warning(
            "Skipping webhook action in rule '%s' because aiohttp is not installed",
            rule_name,
        )
        return

    url = action.get("url")
    if not url:
        LOGGER.warning("Webhook action missing URL in rule '%s'", rule_name)
        return

    method = str(action.get("method", "POST")).upper()
    headers = _render_template(action.get("headers", {}), payload)
    json_body = None
    data_body = None

    if "json" in action:
        json_body = _render_template(action.get("json"), payload)
    elif "data" in action:
        data_body = _render_template(action.get("data"), payload)
    else:
        data_body = _render_template(action.get("body", {}), payload)

    timeout = aiohttp.ClientTimeout(total=float(action.get("timeout", 5.0)))

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(
            method,
            _render_template(url, payload),
            json=json_body,
            data=data_body,
            headers=headers,
        ) as response:
            LOGGER.info(
                "Webhook action '%s' responded with %s",
                rule_name,
                response.status,
            )


async def _action_chat(
    action: Dict[str, Any], context: PluginContext, payload: Dict[str, Any]
) -> None:
    message = _render_template(action.get("message", ""), payload)
    if not message:
        return

    rendered = {
        "action": "chat.message",
        "message": message,
        "user": _render_template(action.get("user", "automation"), payload),
    }
    await context.emit_event("automation.action", rendered)


async def _action_overlay(
    action: Dict[str, Any], context: PluginContext, payload: Dict[str, Any]
) -> None:
    rendered = {
        "action": "overlay",
        "template": _render_template(action.get("template", "default"), payload),
        "duration": action.get("duration"),
    }
    extra = _render_template(action.get("payload", {}), payload)
    if isinstance(extra, dict):
        rendered.update(extra)
    await context.emit_event("automation.action", rendered)


async def _action_media(
    action: Dict[str, Any], context: PluginContext, payload: Dict[str, Any]
) -> None:
    rendered = _render_template(action.get("payload", {}), payload)
    if not isinstance(rendered, dict):
        rendered = {}
    target_action = action.get("action") or action.get("media_action")
    if not target_action:
        target_action = action.get("type", "media")
    rendered.setdefault("action", target_action)
    await context.emit_event("automation.action", rendered)


def _render_template(template: Any, payload: Dict[str, Any]) -> Any:
    if isinstance(template, str):
        return template.format_map(_SafeDict(payload))
    if isinstance(template, dict):
        return {key: _render_template(value, payload) for key, value in template.items()}
    if isinstance(template, list):
        return [_render_template(item, payload) for item in template]
    return template


def _load_rules(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        path.write_text(
            json.dumps(_DEFAULT_RULES, indent=2),
            encoding="utf-8",
        )
        return list(_DEFAULT_RULES)

    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOGGER.warning("Invalid automation rules at %s; using defaults", path)
        return list(_DEFAULT_RULES)

    if not isinstance(rules, list):
        LOGGER.warning("Automation rules file must contain a list; using defaults")
        return list(_DEFAULT_RULES)

    cleaned: List[Dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if "name" not in rule or "event" not in rule:
            LOGGER.warning("Skipping automation rule missing required keys: %s", rule)
            continue
        rule.setdefault("conditions", {})
        rule.setdefault("actions", [])
        cleaned.append(rule)

    return cleaned or list(_DEFAULT_RULES)


def _parse_rules(rules: Iterable[Dict[str, Any]]) -> List[AutomationRule]:
    parsed: List[AutomationRule] = []
    for rule in rules:
        parsed.append(
            AutomationRule(
                name=rule["name"],
                event=rule.get("event", "*"),
                conditions=dict(rule.get("conditions", {})),
                actions=list(rule.get("actions", [])),
            )
        )
    return parsed

