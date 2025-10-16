# Electrum Command Center — Next Steps Assessment

This document summarizes the current state of the Command Center core and highlights the most impactful next tasks. It is based on the initial project objectives, the existing scaffolding in `core/`, and the newly fleshed-out plugins under `plugins/`.

## Current Implementation Snapshot
- **Wallet Adapter**: Bridges Electrum's callback manager when a wallet/network is supplied, translating `new_transaction`, `wallet_updated`, and `network_updated` events into the Command Center schema while retaining simulation helpers for development.【F:electrum_command_center/core/wallet_adapter.py†L1-L247】
- **Event Dispatcher**: Fans out wallet signals to multiple subscribers through asyncio queues, tracks per-subscriber queue occupancy, exposes back-pressure controls, and provides a callback factory used by the adapter.【F:electrum_command_center/core/event_dispatcher.py†L1-L151】
- **Plugin Manager**: Discovers plugin `init.py` modules, validates metadata, spawns per-event workers, supports reload/unload flows for dev hot reloading, injects lifecycle-aware `PluginContext` objects, and surfaces plugin health/dispatcher metrics for remote introspection.【F:electrum_command_center/core/plugin_manager.py†L1-L490】【F:electrum_command_center/core/plugin_context.py†L1-L60】
- **Demo Entry Point**: Boots the adapter, dispatcher, plugin manager, and auxiliary services, then simulates a `transaction_received` event to demonstrate the pipeline end-to-end while reporting dispatcher metrics.【F:electrum_command_center/core/main.py†L1-L72】
- **Built-In Plugins**: The stream bridge exposes a dedicated overlay WebSocket feed, the chat server serves a local HTML/JS interface backed by a WebSocket hub, and the automation engine evaluates JSON-defined rules to emit follow-up actions.【F:electrum_command_center/plugins/stream_bridge/init.py†L1-L189】【F:electrum_command_center/plugins/chat_server/init.py†L1-L266】【F:electrum_command_center/plugins/automation/init.py†L1-L260】

## Gaps Against the Initial Brief
1. **Real Wallet Integration**: The adapter still uses simulated events; integration with the private Electrum fork and its event surface remains outstanding.
2. **System Communications Layer**: The core WebSocket service remains optional at runtime until the `websockets` dependency is available; richer authentication, resilience, and potential REST interop are still outstanding.【F:electrum_command_center/core/websocket_server.py†L1-L212】
3. **Plugin Capabilities**: Overlay and chat servers currently depend on the external `websockets` library; fallbacks, client authentication, persistence, and richer integrations with OBS/Twitch remain to be implemented.
4. **Runtime Tooling**: Hot-reload support now exists when `watchfiles` is installed, yet deeper plugin status reporting, telemetry surfacing, and remote management flows are still outstanding.
5. **Testing & CI**: Automated coverage, linting, and type checking are not configured, risking regressions as functionality grows.

## Recommended Immediate Actions

✅ **Completed**

1. **Electrum Adapter Integration** — the adapter now exposes structured payload schemas, merges caller-provided metadata, and automatically retries callback registration if Electrum rebinding fails.
2. **Event Dispatcher Observability** — metrics are documented in `docs/dispatcher_metrics.md`, available via the CLI and WebSocket APIs, and covered by pytest-asyncio tests.
3. **WebSocket + Transport Hardening** — all transports require tokens, expose structured errors, support client reconnection by ID, and surface health snapshots (dispatcher, plugin lifecycle, connection counts).
4. **Automation & Plugin Integrations** — the automation engine supports webhook, chat, overlay, and media actions, performs live rule reloads, and bridges its events to the stream bridge and chat server transports.
5. **Developer Experience & Quality Gates** — plugin health metrics are reachable through the WebSocket API, pytest coverage spans dispatcher/plugin manager/automation flows, and ruff/mypy/pyright configurations live in `pyproject.toml` and `pyrightconfig.json`.
6. **Telemetry Persistence** — dispatcher and plugin health snapshots are archived in-memory, service health events are recorded, and the WebSocket/CLI interfaces expose historical telemetry for inspection.

### Follow-up Focus Areas
1. **Wallet + Network Resilience** — bubble Electrum disconnect/reconnect events through the dispatcher so transports can reflect degraded service states.
3. **Secrets & Configuration** — move WebSocket/chat tokens into a managed settings store with reload support and provide tooling for secure rotation.
4. **Frontend Enablement** — begin scaffolding the local dashboard leveraging the hardened WebSocket APIs and automation outputs.

## Supporting Considerations
- Document the event payload contracts so plugins and external clients remain in sync.
- Plan for configuration management (e.g., `settings.json`) to store plugin options, WebSocket auth, and rule definitions.
- Maintain modular boundaries: Electrum-specific logic inside the adapter, communication services in `core/`, and domain behavior within plugins.

Executing the immediate actions above will align the project with the original architecture brief while keeping the codebase ready for future Lightning, Nostr, and analytics extensions.
