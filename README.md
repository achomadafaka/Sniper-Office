# Sniper-Office

A small runtime for agents that must not lose or repeat work: durable turns, one supervisor at a time, tools the
host owns, a receipt for every tool call, tickets to humans, written work, and memory with a known-time cut.

It is the storage and control layer only. It makes no model calls, starts no processes and opens no network
connection. You supply a `step` function (your model call) and the tools.

Python 3.11 or newer. No dependencies outside the standard library. SQLite needs FTS5 and JSON functions, which
stock CPython builds include.

Provenance: copied from the `sniper/office/` package of the private Sniper repository at commit `f07f79614` (2026-11-25), then made generic. Stage 1 of the plan in `/home/bigpapi/Desktop/office-extraction/DESIGN.md`.

## Try it

```bash
PYTHONPATH=. python3 examples/two_agents.py
```

Expected output (the receipt ids differ on each run, because the store file is new):

```
ada completed {'outcome': 'success', 'receipt': '7c4379ae7611', 'result': {'items': 3}}
bo completed {'outcome': 'refused', 'receipt': '71c948b3e9f5', 'result': {'reason': 'tool_denied'}}
receipt chain intact: (True, None)
after kill: running
after reconcile: partial deadline
old generation refused: StaleGeneration
```

What it shows: two agents each run one turn. One role may call the tool, the other is refused, and both outcomes
are written as receipts in a hash chain. A turn whose process dies stays `running` until a new supervisor takes
the lease and reconciles it to `partial`. It is never invented as completed. The old supervisor's generation can
no longer write.

## Tests

```bash
python3 -m pytest
```

## Modules

| Module | What it holds |
|---|---|
| `store` | SQLite store. Append-only tables are hash chains with update and delete blocked by triggers. `extend_schema()` adds a project's own tables. |
| `supervisor` | `Lease` (one holder, fenced by generation), `Supervisor.run_turn`, `apply_action` (each action runs at most once), `reconcile`. |
| `broker` | `Broker.execute`: checks the request against the caller's role, runs the host's handler, writes a `Receipt` before returning. `check_claim` verifies a value against a receipt. |
| `capabilities` | The role-to-tool table and the cut checks. Empty until configured. |
| `tickets` | Requests from agents to humans: propose, approve, queue, acknowledge, resolve, with an outbox. |
| `work` | Messages and tasks between agents, recorded as receipts. |
| `memory`, `memory_store` | Entries stamped with the time they became known, supersession, sealed snapshots, full-text ranking. |
| `runtime_observations` | Model-call and hand-off rows for reporting. Never a source of authority. |
| `loops` | Stable turn ids per agent, generation and time slot; a runaway cap. |
| `clock` | `corrected_now(path, host_now=...)`: returns `None` when the correction sample is missing or stale. |
| `launcher_diagnostics` | Redaction of credentials from captured child output. |
| `config` | Loads one JSON file, optionally pinned by SHA-256, and applies it to the modules above. |

## Configuration

Nothing project-specific is in the code. One JSON file names the roles, which tools each role may call, and any
extra tables. `config/office.example.json` is a working example and is what the tests use.

```python
from sniper_office import config, store
config.load('office.json', expected_sha256=PINNED)   # before open_store
conn = store.open_store('data/office.db')
```

| Key | Effect | Default when missing |
|---|---|---|
| `roles` | role name to list of tool names | no role may call any tool |
| `cut_free_tools` | tools allowed when the cut has no `slot_cut` | none |
| `role_aliases` | stored role name to the name used in a turn | the stored name, lower-cased |
| `deferrable_reasons` | reasons that let a first step defer the turn | `work_unknown` |
| `memory` | `roles`, `quarantined`, `source_keys` | every retrieval is refused |
| `tickets` | `crew_ids`, `approver_role`, `queue_roles` | nobody can acknowledge a ticket |
| `handoff_kinds` | `[reference kind, receiving role, event kind]` triples | `<reference kind>_response` |
| `tables` | `append_only`, `private`, `columns`, `indexes` | the built-in tables only |

The file is trusted input: column definitions and index statements in `tables` run as SQL. An unknown key is
refused. With `expected_sha256` given, a changed file is refused and nothing is applied.

Configuration is process-wide (module-level tables). Load it once at start-up.

## Differences from the source

A project that used the source package must set these in its config to get the old behaviour.

- The role table, memory roles, ticket crew ids and the role alias were literals. They are config keys now.
- The store created seven project tables and several project indexes. They are gone from the default schema;
  declare them under `tables`. Built-in table names and columns are unchanged, so an existing store file opens.
- The turn deferral reason was one fixed project string. It is `deferrable_reasons` now.
- `runtime_observations.Buffer` took a second database and read order rows from it. It now takes `hooks`:
  callables `(buffer, now)` that run inside the flush transaction.
- The ticket delivery status for a queued hand-over is `crew_queued`.
- Memory entries are excluded when they share a `source_keys` value with the candidate; the default keys are
  `subject`, `creator`, `minute`.

## Not included

Sandboxes and long-running agent sessions, model transports and credentials, token budgets, organisation and
epoch management, evaluation and promotion, any web UI, any service unit. Those are later stages of the plan.
