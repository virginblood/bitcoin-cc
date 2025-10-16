# Electrum Command Center Roadmap

This document outlines recommended next steps that build on the core event and plugin scaffolding already implemented. It is derived from the initial project brief and the current repository state.

## 1. Stabilize Core Services ✅
- **Wallet Adapter Integration**: ✅ The adapter now binds to Electrum's callback manager when a wallet/network is attached, translates real ``new_transaction`` / ``wallet_updated`` events, and retains simulation helpers for dev workflows.
- **Event Dispatcher Hardening**: ✅ Per-subscriber queue metadata, overflow diagnostics, and occupancy snapshots are available so slow consumers and back-pressure can be detected quickly.
- **Plugin Lifecycle Enhancements**: ✅ Plugins track health status, failure counts, and per-event subscription IDs while the manager exposes dispatcher metrics for remote introspection.

## 2. WebSocket & API Layer
- **Local WebSocket Hub**: Build the `websocket_server.py` service for GUI and plugin communications, including authentication tokens and heartbeat pings.
- **REST Bridge (Optional)**: Scaffold a lightweight REST interface for external automations, sharing the same event bus.

## 3. Built-In Plugin Expansion
- **Stream Bridge**: Define OBS overlay message schema, add throttling, and prepare hooks for third-party streaming platforms.
- **Chat Server**: Stand up the local WebSocket chat backend plus a minimal HTML/JS client served from the core app.
- **Automation Engine**: Design rule definitions (YAML/JSON), action executors, and persistence for creator-defined workflows.

## 4. Frontend Foundations
- **Dashboard MVP**: Decide between PyQt and Electron, then scaffold the wallet/chat/stream/automation tabs with WebSocket subscriptions.
- **Real-Time Views**: Surface balance, latest transactions, plugin statuses, and recent automation triggers.

## 5. Testing & Tooling
- **Async Test Suite**: Introduce pytest-asyncio coverage for dispatcher, plugin manager, and adapter integration paths.
- **CI Pipeline**: Configure linting (ruff/black), type checking (mypy/pyright), and unit tests for continuous integration.

## 6. Future Extensions (Reference)
- Lightning integration via LNbits or LND gRPC.
- Nostr chat federation.
- Analytics dashboard and enterprise API surface.

Each milestone should maintain the modular, local-first architecture and preserve compatibility with the private Electrum fork.
