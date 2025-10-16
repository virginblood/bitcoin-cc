# Event Dispatcher Metrics

The `EventDispatcher` maintains lightweight counters describing how wallet
signals propagate through the Command Center.  These metrics are exported over
the WebSocket management API and exposed locally via the CLI tooling introduced
in this change set.

## Snapshot Payloads

* `events.delivered` — cumulative count of payloads that were delivered to
  subscribers keyed by event name.
* `events.dropped` — count of payloads that were discarded because their target
  queue was full.  The applied overflow strategy determines whether new or old
  messages were dropped.
* `subscribers[]` — a list of subscriber queue descriptors containing:
  * `event`: subscription key (or `"*"` for wildcard queues)
  * `subscriber`: identifier supplied during registration
  * `size`: current queue depth at the time the snapshot was produced
  * `maxsize`: configured maximum queue size.  Zero indicates an unbounded
    queue.

Snapshots are monotonic until `reset_metrics()` is called.  Consumers that wish
 to derive per-interval rates should cache successive snapshots and compute
 their own deltas.

## Accessing the Metrics

1. Connect to the Command Center WebSocket API and send a `{"action":
   "health"}` request to receive the latest dispatcher metrics alongside plugin
   lifecycle data.
2. Run `python -m electrum_command_center.tools.dispatcher_cli` to fetch the
   same payload from a shell.

Both interfaces return JSON with a top-level `dispatcher` object that mirrors the
structure above.
