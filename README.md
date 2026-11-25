# Sniper-Office

A small runtime for agents that must not lose or repeat work: durable turns, one supervisor at a time, tools the
host owns, a receipt for every tool call, tickets to humans, written work, and memory with a known-time cut.

It is the storage and control layer only. It makes no model calls, starts no processes and opens no network
connection. You supply a `step` function (your model call) and the tools.

Python 3.11 or newer. No dependencies outside the standard library. SQLite needs FTS5 and JSON functions, which
stock CPython builds include.

Provenance: copied from the `sniper/office/` package of the private Sniper repository at commit `f07f79614` (2026-11-25), then made generic. The plan and its stages are in `docs/DESIGN.md`.

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

Seat runtime: long-running agent sessions in a sandbox. `docs/SETUP.md` is the host setup guide.

| Module | What it holds |
|---|---|
| `seat_main` | Entry point for one seat's host process: `python3 -m sniper_office.seat_main --config seat.json`. |
| `seat_host` | When a seat wakes, what it is told, its budget, session rotation and handover. |
| `seat_tools` | The tool server a seat talks to. Built-in tools: `shell`, `browse`, `web_search`, `db_read`, `mail_send`, `mail_read`, `sleep`, `request`, `notify_operator`. |
| `plugins` | How a project adds its own tools, wake reasons, deny hosts, labels, retention rules and notifications. |
| `seat_db` | `db_read`: one SELECT over copies of allowed tables and columns, rows up to now only. |
| `seat_publish` | Publishes the package, a project tree and a Python runtime for the sandbox user; refuses secrets. |
| `seat_sandbox` | `bwrap` and `systemd-run` arguments: one sandbox that holds the model credential and no tools, one that holds tools and no credential. Neither has a network route out. |
| `egress_proxy`, `netguard` | Host-side proxy on a Unix socket: public addresses only, pinned IPs, a host denylist, per-host rate limits. |
| `model_proxy` | Host-side proxy for a model provider; the sandbox holds a placeholder token and the real one stays on the host. |
| `search_broker`, `seat_search`, `seat_browse`, `tools/browse` | Web search and page reads through the proxy, with a shared cache and daily budget. |
| `seat_events` | Seat mailbox, roster and event log. |
| `quota_measurement`, `quota_read` | Token usage per account and seat. |
| `seat_retention` | Bounded deletes for every table and log the seat runtime writes. |
| `seat_child`, `codex_child`, `seat_forward` | What runs inside the sandbox for one wake of one seat. |
| `claude_seat_auth`, `launcher_client` | Read-only credential snapshots; a token is never refreshed or written back. |

One-shot model calls and organisation (stage 3). An alternative to seats: each model call is one disposable child
process started by a launcher that owns the credentials.

| Module | What it holds |
|---|---|
| `supervisor_main` | Entry point for the supervisor process: `python3 -m sniper_office.supervisor_main --runtime-factory yourmodule:build`. |
| `models` | `call()`: one attempt, one typed result envelope (outcome, usage, served model, billing basis). No transport configured means refused. Needs `jsonschema`. |
| `scheduler` | Bounded admission of model calls per provider. |
| `launcher_main`, `child_launcher`, `launcher_runtime`, `launcher_bindings`, `launcher_client`, `supervisor_client` | A no-network launcher on a Unix socket. Only the pinned supervisor may ask it for a child; it checks caps, guard and clock first. |
| `host_guard` | The two checks before any child: resource caps really applied, and a fresh "ready" from an outside guard file. |
| `model_child`, `claude_compat`, `codex_cli` | What runs as the child: one Claude Agent SDK call or one Codex CLI call, no tools. |
| `model_auth`, `host_auth`, `codex_auth` | Credential snapshots for a child; a rotated Codex token is written back only for the same account. |
| `org`, `roster`, `shifts` | Seats from an org file, hire and fire with epochs, shift boundaries and handover notes. Examples in `config/`. |

Evaluation and promotion (stage 4). `docs/EVALUATION.md` explains the procedure.

| Module | What it holds |
|---|---|
| `evaluation` | Design file, case log, grading into a separate file, the verdict (`pass`, `pass_pending_replication`, `pass_unproven_custody`, `fail`, `no_verdict`), release of aggregates, and the promotion ticket. Standard library only. |
| `roster` (`open_evaluated_epoch`, `verify_nominee`) | Freezes one configuration, numbers the attempt, spends significance, appends to the trial registry. Any roster, prompt or model change breaks the epoch. |
| `nominee` | Checks the frozen configuration against the files, the live roster and the sealed memory snapshot. |
| `launcher_policy` | One-use host authority for a freeze or re-check, with the read-only config proof. |

```bash
PYTHONPATH=. python3 examples/evaluation_demo.py
```

That runs a made-up team through two evaluated epochs and files the promotion ticket. A pass only ever files a ticket.

Operator page (stage 5a to 5c). `docs/UI.md` is the design.

| Module | What it holds |
|---|---|
| `ui` | A small local web server, standard library only. Overview of every seat, one seat's timeline, the mail. Two actions with a token: write to a seat, clear a park. |
| `views` | The read models behind it: bounded, read-only, unknown never shown as zero. |

```bash
PYTHONPATH=. python3 examples/ui_demo_data.py /tmp/office-demo
```

```bash
PYTHONPATH=. python3 -m sniper_office.ui --office-db /tmp/office-demo/office.db --quota-db /tmp/office-demo/quota.db
```

Then open http://127.0.0.1:8765. Add `--token-file` (a file only you can read, at least 24 characters) to enable the
two actions; `--port` if 8765 is taken. Not built yet: requests, budget, evaluation and health views, project panels.

The seat runtime needs Linux, `bwrap`, systemd user units and a dedicated system user (default `office`). It also
needs the `cryptography` package on the host, and `claude-agent-sdk` and `playwright` inside the published runtime.
`tests/test_walls.py` and `tests/test_browse_security.py` run the real sandbox when that user and runtime exist and
skip otherwise.

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
| `crew_roles` | who may mail seats as `crew:<role>` | nobody |
| `host` | `bridge`, `home`, `user` for the sandbox | `/srv/sniper-office`, `/var/lib/sniper-office`, `office` |

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
- The tool server had about 30 project tools built in. They are gone; a project registers its own through `plugins`.
- The seat host woke on project events and read a project ledger before rotating. Those are `add_wake_source` and
  `add_busy_check` now. The guard on another process's heartbeat is optional (`heartbeat`), its reasons are
  `guard_heartbeat_unreadable`, `guard_not_ok`, `guard_heartbeat_stale`, and the key it reads is `heartbeat_ok_key`.
- `db_read` took a fixed table list. It takes a spec file (`data_sources`) and no longer has per-table notes,
  day-partitioned files or text truncation markers; a project that needs those replaces the tool.
- Retention no longer touches project tables; `add_retention` adds rules. The page log is kept 90 days.
- The entry point is `python3 -m sniper_office.seat_main`. The publish step always places this package in the release.
- `models.call` had two built-in transports that imported project code. `models.TRANSPORTS` is empty; pass
  `transport=` or fill it at start-up.
- `org.expand` and `org.validate` knew three role names. The pod template is now a list of
  `{role, per_pod, families, attribute, values}` and the limits use `role_minimums` and `role_families_required`.
- `roster` lost evaluated epochs, nominee verification and the pilot seed; they return with evaluation.
- The launcher checked a fixed control directory, unit file and library path. Set them with
  `launcher_runtime.configure(...)`; unset means refused. Expected caps come from `office_caps` in the launcher config.
- The Codex lock path has no default: `codex_cli.codex_slot(path=...)`.
- Memory entries are excluded when they share a `source_keys` value with the candidate; the default keys are
  `subject`, `creator`, `minute`.

## Not included

A watchdog that writes the guard file, service units for the supervisor and launcher, a grader entry point, an
auditor that pins chain heads outside the database, the optional bootstrap and calibration statistics, and the web
UI.

Not yet proven: a full wake from this package on a real host (publish, sandbox, model call, tool call, mail). The
parts are tested, and the sandbox tests run for real, but the end-to-end run has not been done.
