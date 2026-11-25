# Extracting the Office from Sniper: design note

This note describes the private source repository this package was extracted from. It names that repository's files and its
trading domain on purpose, so it is the one file the project-word test skips. Remove it before this repository is made public.

Status: scoping only. No code moved or copied. Written 2026-11-25 against Sniper `main` at `f07f79614`.
All paths are relative to the root of the Sniper repository unless they start with `/`.
Every `file:line` below comes from a file read or a grep run for this note.

## Findings that change the brief

1. **The Office is not running on this machine.** Every `office-*` user unit is installed but `disabled` and
   `inactive` (`systemctl --user is-active office-seat@head office-paper-watcher-v2 office-supervisor
   office-watchdog sniper-office-intel-hl` returns `inactive` for all). The Office cron lines carry the marker
   `PAUSED-OFFICE-2026-11-01-OPERATOR-until-resume`. Seat state files under `data/office/v2/` were last written
   2026-11-01, `data/office/office.db` on 2026-11-09. No Sniper trading process is running here either. If the
   Office is live on another host, this note could not see it.
2. **`sniper/office/` is three things in one folder**, not one runtime:
   - a durable-turn runtime (called v1 here): supervisor, lease, typed tool requests, receipts, tickets, memory;
   - a persistent-seat runtime (v2), which replaced v1 as the way seats run
     (`docs/superpowers/specs/2026-10-30-office-v2-persistent-seats-design.md:3-5`): sandboxes, egress proxy, tool
     server, mailbox, budgets;
   - trading-domain code that happens to live in the same package: about 24,600 of the 33,268 lines.
3. **A third seat runtime exists outside the Office**, in `sniper/research/pm_desk/` (`seat_sessions.py`,
   `seat_bus.py`, `seat_agent.py`, `seat_wall.py`, `isolated_seat_tools.py`, `quant_egress.py`). It is the most
   recently changed code (2026-11-22) and reuses only `sniper.office.egress_proxy`, `netguard` and `clock`
   (`sniper/research/pm_desk/quant_egress.py:19`, `run_desk.py`, `run_tape.py`). It does not use the Office store,
   supervisor or seat host.
4. **The durable core already has no outside imports.** The import closure of each of `store`, `supervisor`,
   `broker`, `tickets`, `work`, `memory_store`, `roster`, `child_launcher`, `egress_proxy`, `search_broker`,
   `seat_db` and `quota_measurement` contains no `sniper.services`, `sniper.persistence` or `sniper.research`
   module. The coupling sits in two composition modules (`seat_host.py`, `seat_tools.py`), the model transports,
   and the paper book.

## 1. What the Office core is

Generic means: no mention of coins, wallets, pools, venues or the crew's names. Sizes are lines of Python.

### 1a. Durable core (v1), 33 modules, about 4,600 lines

| Piece | Module | Lines | Notes |
|---|---|---|---|
| Store, append-only chains | `sniper/office/store.py` | 174 | Canonical JSON, `write_transaction`, hash-chained append-only tables. Generic mechanism, but the table list at `store.py:14-34` names domain tables (`opportunity_log` has `network_id`, `mint`). |
| Turns, lease, fenced actions | `sniper/office/supervisor.py` | 469 | `Lease` (`:45`), `Supervisor.run_turn` (`:307`), `apply_action` (`:216`), `reconcile` (`:289`), `run_child` (`:423`). Imports only `store` (`:14`). |
| Typed tool requests and receipts | `sniper/office/broker.py` | 208 | `ToolRequest`, `ToolResult`, `Tool`, `Receipt`, `Broker`. `Receipt` carries `slot_cut` and `network_id` (`:50-65`), which are chain words. |
| Capabilities | `sniper/office/capabilities.py` | 95 | Mechanism is generic. The role table `CAPS` (`:7-18`) is hard-coded with `scout`, `judge`, `risk`, `trader`, and tools like `book.submit`. |
| Tickets (requests to humans) | `sniper/office/tickets.py` | 161 | Workflow states are generic; roles `head`, `crew`, `supervisor` are literals (`:88-95`). |
| Written work, tasks, messages | `sniper/office/work.py` | 255 | No outside imports. |
| Memory | `sniper/office/memory.py`, `memory_store.py` | 102 + 168 | Known-time retrieval, supersession, sealed snapshots. `ROLES` is a literal set (`memory.py:51-52`). |
| Org, roster, epochs | `sniper/office/org.py`, `roster.py`, `shifts.py` | 87 + 381 + 94 | Hire, fire, epochs. `roster.py:333` pins the slot set `head, scout, judge_claude, judge_gpt`; `roster.py:97-98` lazily imports `nominee`, `launcher_policy`, `readiness.verdict`. |
| Scheduling | `sniper/office/loops.py`, `scheduler.py` | 56 + 140 | Stable turn ids, bounded admission. |
| Clock | `sniper/office/clock.py` | 21 | `corrected_now(path, host_now)`; path is a parameter. |
| Model launch and auth (v1) | `child_launcher.py`, `launcher_*.py` (5), `model_child.py`, `models.py`, `model_auth.py`, `host_auth.py`, `codex_auth.py`, `claude_compat.py`, `model_diagnostics.py`, `supervisor_client.py` | about 1,800 | One model call per disposable child. Superseded by v2 for running seats; kept in Sniper "for rollback". |

### 1b. Seat runtime (v2), 21 modules, about 4,000 lines

| Piece | Module | Lines | Notes |
|---|---|---|---|
| Seat lifecycle, wake, budget, rotation, handover | `sniper/office/seat_host.py` | 705 | `class Seat` (`:272`), `wake` (`:468`), `rotate` (`:631`), `park_reason` (`:82`). |
| Tool server (MCP over a Unix socket) | `sniper/office/seat_tools.py` | 587 | One hard-coded `TOOLS` dict (`:56-97`) mixing generic and trading tools. |
| Sandboxes | `sniper/office/seat_sandbox.py` | 56 | `bwrap` arguments, `systemd-run` as user `office`. Paths are constants (`:4-5`). |
| In-sandbox children | `seat_child.py`, `codex_child.py`, `seat_forward.py`, `seat_browse.py`, `seat_search.py` | 60 + 92 + 45 + 33 + 148 | Stdlib plus the Agent SDK. |
| Egress | `egress_proxy.py`, `netguard.py`, `search_broker.py`, `model_proxy.py` | 342 + 97 + 148 + 172 | Public-only pinned-IP transport, denylist, per-host rate. No outside imports. |
| Mailbox, roster, events | `seat_events.py`, `seat_work.py`, `seat_feed.py` | 200 + 71 + 184 | Tables `office_v2_events`, `office_v2_roster`. `CREW_ROLES` is a literal (`seat_events.py:97`). |
| Budgets | `quota_measurement.py` | 270 | Token counters per account and seat. |
| Read-only database tool | `seat_db.py` | 310 | Mechanism is generic (copy rows up to the cut into a private in-memory database). The allowlist `ALLOWED` (`:34` onward) names Sniper tables. |
| Retention, publish | `seat_retention.py`, `seat_publish.py` | 151 + 139 | Publishes a git-tracked tree and a Python runtime for the `office` user. |
| Subscription auth snapshots | `claude_seat_auth.py` | 60 | Never refreshes a token. |

### 1c. Evaluation and promotion: not generic today

The brief lists paper evaluation and promotion controls as core. In the code they are trading code:

- The paper book imports the memecoin fill model at module top: `sniper/office/ledger.py:36-38`
  (`memecoin_desk_ps_paper`, `memecoin_desk_paper.ENTRY_DELAY_S, EXEC_COST_SOL, EXIT_DELAY_S`,
  `memecoin_desk_ps_tape.canonical_pool`), and uses those names 17 times.
- `grader.py` (533), `null_arm.py`, `replay_fill.py`, `capture.py`, `outcomes.py`, `eval_*.py` (5 files, 1,869),
  `readiness/` (440) and `risk.py`/`trader.py` implement one preregistered trading experiment.
- What is generic is small and already in 1a: epochs (`roster.py`), sealed views and append-only `grades`,
  `claims`, `decisions` tables (`store.py:14-17`), and the rule that evaluation rows live in a separate file
  (`store.py:3-4`).

### 1d. What stays in Sniper (domain, about 24,600 lines)

- Wallet research: 21 `wallet_*.py` files, 5,416 lines; `chain_data.py`, `funder_lookup.py`, `copy_wallets.py`,
  `networks.py`, `watch_alert.py`, `safety_evidence.py`.
- Paper trading and its experiment: `ledger.py` (1,341), `v2_paper.py`, `v2_orders.py`, `eval_*`, `grader.py`,
  `null_*`, `judge_*`, `risk.py`, `trader.py`, `pods*.py`, `partial_*`, `tools/sheet.py`, `self_sheet.py`,
  `market_data.py`, `pool_state_rpc.py`, `position_charts.py`, `desk_*`, `seat_candidates.py`, `seat_facts.py`,
  `seat_intel.py`, `feed_*`, `pass_replay.py`, `large_buyer_arm.py`, `runner_study.py`.
- Prediction-market desk helpers: 12 `pm_desk_*.py`, `pm_admission.py`, 8 `intel_*.py`; 3,149 lines.
- Composition and one-off transitions: `runtime.py`, `runtime_steps.py`, `probe*.py`, `pilot_*`,
  `epoch_transition.py`, `eval_transition.py`, `v2_transition.py`, `v2_explore.py`, `training*.py`.
- Operator views: `report.py`, `eval_report.py` (1,206), `explore_report.py`, `operator_view.py`, `floor*.py`,
  `reporting_view.py`, `intel_dashboard.py`.
- Host-guard designs not wired into a service: `planned_*.py`, `root_guardian.py`, `research_vm.py`,
  `config_write_probe.py`.

## 2. Dependencies on the rest of Sniper

Verdict key: **core** = belongs in the new repository; **plug-in** = stays in Sniper and registers with the
Office; **copy** = small generic helper to copy into the new repository; **stays** = Sniper-only, not registered.

### 2a. `sniper.services.*` (imports from inside `sniper/office/`)

| Where | Imports | Verdict |
|---|---|---|
| `models.py:53`, `models.py:156` | `memecoin_desk_gpt.call_model`, `memecoin_desk_model_echo.echo_ok` | **copy** the transport behind an adapter interface; the echo check is generic. |
| `ledger.py:36-38` | `memecoin_desk_ps_paper`, `memecoin_desk_paper`, `memecoin_desk_ps_tape` | **plug-in** (the whole ledger). |
| `tools/sheet.py:24-27` | `memecoin_desk_contract`, `_d_blocks`, `_ps_facts`, `_ps_trending` | **plug-in** tool. |
| `partial_order.py:24`, `partial_sheet.py:166`, `self_sheet.py:79`, `desk_near_pass.py:154` | `memecoin_desk_contract`, `memecoin_desk_judge.intake_mint_of` | **plug-in**. |
| `v2_paper.py:235,263,320,342,370,454` | `memecoin_desk_contract`, `memecoin_desk_intake.clock_sample` | **plug-in**. The clock sample is the only generic part; `clock.py` already covers it. |
| `runtime.py:213-214`, `operator_view.py:16,110`, `probe_runtime.py:135` | `memecoin_desk_asof`, `memecoin_desk_intake.clock_sample` | **stays** (composition and views). |
| `chain_data.py:114,130` | `memecoin_research_rpc.funded_overlap`, `memecoin_telegram.send_telegram` | **plug-in**. The operator page is a notifier hook in core. |
| `wallet_watch.py:322-330`, `wallet_watch_collect.py:23,318-319,420-421`, `wallet_campaign.py:149`, `wallet_launch_proof.py:4`, `wallet_stream_proof.py:13`, `funder_lookup.py:8`, `copy_wallets.py:7`, `pool_state_rpc.py:136`, `seat_facts.py:8`, `position_charts.py:166,320` | `memecoin_desk_txshape`, `memecoin_fee_reconciler`, `memecoin_cluster_rpc`, `wallet_identity`, `memecoin_desk_universe`, `memecoin_desk_backfill_history`, `memecoin_shyft_confirmed`, `memecoin_chains`, `memecoin_desk_ps_pool_read`, `memecoin_desk_ps_tape` | **plug-in** or **stays**. |

### 2b. `sniper.persistence.*`

| Where | Imports | Verdict |
|---|---|---|
| `runtime.py:210-212` | `db_memecoin_desk`, `db_core.get_readonly_connection`, `db_memecoin.desk_buyer_ledger` | **stays**. |
| `runtime_steps.py:103`, `eval_transition.py:84`, `v2_paper.py:243,309` | `db_memecoin_desk` | **stays** / **plug-in**. |
| `probe_runtime.py:121-122` | `db_core`, `db_memecoin` | **stays**. |
| `wallet_activity.py:8` | `db_core.get_readonly_connection` | **plug-in** tool (`wallet_activity`). |

No core module imports `sniper.persistence`. The Office's own databases are plain `sqlite3`.

### 2c. `sniper.research.*` and `sniper.feeds.*`

| Where | Imports | Verdict |
|---|---|---|
| `intel_host.py:10`, `intel_runtime.py:168,182`, `intel_service.py:291` | `sniper.research.pm_desk` (`tape`, `pm_frontier.proof`, `pm_bucket.PriorityBucket`, `run_pm_desk.FEEDS`) | **stays**. |
| `wallet_campaign.py:150-151` | `sniper.research.memecoin_opportunity` | **stays**. |
| `wallet_watch_collect.py:496`, `wallet_campaign.py:189` | `sniper.feeds.yellowstone.geyser_pb2` | **stays**. |

### 2d. `scripts.*` imported by the package

| Where | Imports | Verdict |
|---|---|---|
| `codex_auth.py:9` (uses `codex_slot`, `prepare_codex_home`, `sync_auth_back` at `:52-84`), `model_child.py:15` (same, plus `minimal_env`, `:60-72`) | `scripts.memecoin_desk_checker` | **copy** these four helpers; they are generic subscription-auth isolation. |
| `model_child.py:14` (uses `_codex_json_locked`, `:74`) | `scripts.research.judge_gpt_replay` | **copy**. |
| `models.py:47` | `scripts.memecoin_desk_judge_worker.sdk_call_model` | **copy** behind the adapter. Its own import closure pulls in the funded runner modules, so it must not be imported as is. |
| `runtime.py:21`, `pilot_caps.py:7` | `scripts.office_watchdog` | **stays** with `runtime.py`. `scripts/office_watchdog.py` itself has no outside imports. |
| `runtime.py:352`, `v2_paper.py:262,369`, `operator_view.py:15`, `probe_runtime.py:134` | `scripts.office_paper_watcher.read_fx` | **plug-in**. |
| `codex_child.py:82` | `scripts.office_quota_read` | **core** (52 lines; move into the package). |

### 2e. Reverse dependencies: Sniper code that imports `sniper.office`

These must keep working after the move.

| Importer | Uses | After the move |
|---|---|---|
| `sniper/research/pm_desk/quant_egress.py:19` | `egress_proxy`, `netguard`; also reads `config/office/v2_egress_denylist.json` (`:56`) | Imports the new package. |
| `sniper/research/pm_desk/run_desk.py`, `run_pm_desk.py`, `run_tape.py`; `sniper/web/server.py:1319` | `clock.corrected_now` | Imports the new package. |
| 20 files in `sniper/research/pm_desk/` | `pm_desk_fees`, `pm_desk_call`, `pm_desk_null`, `pm_desk_score`, `pm_desk_hl_catalog`, `pm_desk_venue_hl` | No change. These modules are desk code that sits in the wrong folder. |
| `sniper/routes/api_office.py:7-11,115,156,197,203` | `seat_feed`, `seat_events`, `report`, `operator_view`, `position_charts`, `wallet_network`, `order_price_fields` | `seat_feed`, `seat_events` come from the package; the rest stay. |
| `sniper/routes/api_copy_wallets.py`, `api_networks.py`, `api_runners.py`, `api_core.py`; `sniper/services/memecoin_known_group_tail.py`, `memecoin_known_group_canary.py`; `sniper/research/early_buyer_funders.py` | `copy_wallets`, `networks`, `runner_study`, `intel_dashboard`, `wallet_launch_proof`, `wallet_selector_identity`, `wallet_stream_proof`, `chain_data`, `wallet_network` | No change (domain modules). |

### 2f. Databases

| File | Owner | Verdict |
|---|---|---|
| `data/office/office.db` (176 MB) | `store.py`, `seat_events.py`, `work.py`, `memory_store.py` | **core** schema, data stays where it is. Path is config (`office_db` in `config/office/seat_scout_v2.json`). |
| `data/office/v2/quota.db` | `quota_measurement.py` | **core**. |
| `data/office/v2/explore-ledger/ledger.db`, `data/office/ledger/` | `ledger.py` | **plug-in**. |
| `data/office/v2/chain.db` (7.6 GB), `data/office/wallet_network.db`, `data/office/v2/market.db` | `chain_data.py`, `wallet_network.py`, `market_data.py`; created in `seat_host.py:489-492` | **plug-in**. Core must stop constructing them. |
| `data/memecoin_desk.db`, `sniper_tracker.db` | Sniper; passed as `desk_db`, `funded_db` in seat config; read by `seat_db.py` allowlist and `wallet_activity.py` | **plug-in** data sources. |
| `data/office/outcomes*.db`, `pass_replay*.db`, `early_buyer_funders.db`, `intel-hl-forward-v1/tape.db` | domain scripts | **stays**. |

### 2g. Config

- `config/office/seat_*_v2.json` (7 files): one flat JSON per seat. Generic keys: `actor`, `epoch`, `generation`,
  `family`, `model`, `effort`, budgets, `wake_deadline_s`, `max_turns`, sleeps, `office_db`, `quota_db`,
  `workspace`, `state_dir`, `prompt_file`, `role`, `denylist`, `search_policy`, `clock_file`. Sniper keys:
  `desk_db`, `funded_db`, `heartbeat`, `ledger_db`, `desk_dir`, `book_policy`, `research_env_file`, `brain_env`,
  `funded_config_files`, `runner_pid_file`. **Core** reads the generic keys; a plug-in receives the whole dict.
- `config/office/default_org.json`, `limits.json`, `shifts.json`, `cadence.json`, `search.json`,
  `v2_egress_denylist.json`: shape is **core**, content is Sniper's.
- `config/office/book_policy_pilot.json`, `pods.json`, `metric_registry.json`, `l_null.json`,
  `cost_allocation_rule.json`, `tape_decoder_versions.json`, `pilot_*.json`, `host_guard*.json`,
  `intel_forward_prompt_v1.md`, `prompts/` (4 folders; `office-v2/` holds `scout.md`, `judge.md`, `head.md`,
  `forensics.md`, `narrative.md`, `researcher.md`, `judge_gpt.md`), `proofs/`: **stays**.
- `config/office/runtime.json` (mode 0600, 40 KB, pinned by `runtime.sha256`): v1 composition, **stays**.

### 2h. Environment loading

- No core module reads `.env`. `seat_tools.py:41` reads one variable, `OFFICE_REQUEST_NOTIFY`.
- `chain_data.research_url_of(env_file)` (`chain_data.py:110`) and `operator_page` (`:126`) parse Sniper's `.env`
  for a research RPC key and a Telegram target; called from `seat_host.py:488-489`. **Plug-in**.
- `scripts/office_seat_host.py:48-56` reads the funded runner's `/proc/<pid>/environ` and `.env` to build the
  egress denylist. The mechanism (deny hosts found in named files) is **core**; which files is config.
- `seat_tools.py:471-479` shells out to `scripts/office_brain.py --env-file <brain_env>`. **Plug-in** (brain
  adapter).
- `seat_tools.py:39-53` runs `~/.local/bin/sniper-crew send --to Codex --from Alpha`. Hard-coded crew command and
  names. **Core** gets a notifier hook; the crewlink call moves to Sniper's plug-in.

### 2i. systemd units and host layout

| Item | Verdict |
|---|---|
| `deploy/office/office-seat@.service`, `office-seat-head.service` | **core** as templates. They hard-code `/srv/sniper-office/release`, an absolute interpreter path under the operator's home, `scripts/office_seat_host.py`, `config/office/seat_%i_v2.json`. |
| `deploy/office/office.slice` | **core** template (caps are host-specific). |
| `office-supervisor*.service/.timer`, `office-child-launcher.service`, `office-watchdog.service`, `office-monitor.service` | v1; **core** only if the v1 launcher is extracted (stage 4). |
| `office-paper-watcher*.service` (3), `office-large-buyer-arm.service`, `memecoin-crowd-trigger.service`, `memecoin-large-buyer-pilot.service` | **stays**. |
| `deploy/office/research-vm/`, `README-pilot-prepare.md`, `requirements.txt` (pins `claude-agent-sdk==0.2.174`) | **stays**; the SDK pin moves to the package's dependencies. |
| System user `office`, `/srv/sniper-office` (`release`, `runtime`, `seats`), `/var/lib/sniper-office` | **core** concept. Names are constants at `seat_sandbox.py:4-5` and the unit name prefix `office-seat-` at `seat_host.py` wake (`unit='office-seat-%s-%s'`). They become config with today's values as Sniper's settings. |
| `seat_publish.publish_release` (`seat_publish.py:78-91`) | Publishes the whole Sniper git tree to `/srv/sniper-office/release`; `seat_child.py:8-9` then runs `sys.path.insert(0,'/opt/release'); from sniper.office import ...`. After the move the sandbox children must import the new package from the published runtime (`publish_runtime`, `:107-128`, already pip-installs pinned packages into a private site-packages). |

### 2j. Web UI and static assets

- Routes: `sniper/routes/api_office.py` (223 lines), registered at `sniper/web/server.py:1320-1328`. It also
  serves newer desk views (`:42`, `:66`, `:98` import `sniper.research.pm_desk`).
- Template: `sniper/web/templates/office.html` (25 lines).
- Static: 17 `static/office-*.js` and `.css` files, about 5,600 lines (pixel office, rooms, charts, newsroom).
- Verdict: **stays** in Sniper. The UI is a Flask blueprint inside Sniper's dashboard and half of it shows trading
  views. The package exposes read functions (`seat_feed.read_feed`, `seat_events`); a generic UI is a later,
  separate decision.

### 2k. Scripts and tests

- `scripts/office_*`: 51 files. Core entry points: `office_seat_host.py` (388; needs splitting, see section 3),
  `office_seat_clear.py`, `office_v2_add_seat.py`, `office_crew_dm.py`, `office_quota_read.py`,
  `office_quota_snapshot.py`. v1 entry points: `office_supervisor.py`, `office_child_launcher.py`,
  `office_model_child.py`, `office_watchdog.py`, `office_supervisor_wake.py`. The remaining ~38 are domain
  (wallet, eval, outcome, intel, pilot) and **stay**.
- `tests/office/`: 286 Python files, 47,284 lines. 47 import non-office Sniper modules directly. The tests for
  `store`, `supervisor` (3), `broker`, `tickets`, `work`, `memory` (2), `memory_store`, `org`, `roster`,
  `netguard`, `search_broker`, `scheduler`, `loops`, `clock`, `quota_measurement`, `child_launcher`, `host_auth`,
  `model_auth`, `shifts` import nothing outside the Office. 46 files are wallet tests. Roughly 60 files move with
  the core; the rest stay.

## 3. The seam

One rule: the new package imports nothing from Sniper, and Sniper hands it everything domain-specific at start-up
through one plug-in module named in config.

### 3a. Plug-in module

Seat config gains one key, for example `"plugins": ["sniper.office_plugin"]`. The host imports each named module
and calls `register(host, cfg)`. No entry points, no discovery. `host` exposes five registration calls:

| Call | Replaces | Sniper registers |
|---|---|---|
| `host.add_tool(name, description, schema, handler, group=None)` | the `TOOLS` dict and `_name` methods in `seat_tools.py:56-97, 231-411` | `market_data`, `chain_*` (4), `wallet_activity`, the 8 `NETWORK_TOOLS`, the 14 `PAPER_TOOLS` (`seat_tools.py:31-32`), `brain_read`, `brain_note` |
| `host.add_data_source(name, path, tables)` | `seat_db.ALLOWED` (`seat_db.py:34` on) and `seat_intel.py` | `desk_tickets`, `desk_ticket_heads`, the intel tables |
| `host.add_wake_source(fn)` | ticket, ledger and watch wakes inside `seat_host.py` (`_watch` `:417`, `_postmortems` `:402`, `_order_pending` `:621`) | new ticket on feed, open-position event |
| `host.set_notifier(fn)` | `_ping_alpha` (`seat_tools.py:39-53`) and `chain_data.operator_page` | crewlink send, Telegram page |
| `host.add_deny_hosts(source)` | `scripts/office_seat_host.py:48-56` | hosts parsed from `.env` and the runner's environment |

Core keeps these tools built in, because they have no domain: `shell`, `browse`, `web_search`, `db_read`,
`mail_send`, `mail_read`, `sleep`, `request`, `notify_operator`. Tool groups replace the two hard-coded sets: a
tool registered with `group='paper'` is hidden when its group is not enabled for the seat, which is what
`seat_tools.py:212-215` does today.

### 3b. Names that move to config

- Role names and their tools: `capabilities.CAPS` (`capabilities.py:7-23`) becomes a JSON file loaded and pinned by
  hash, the same way `org.py:22-27` already loads `default_org.json`.
- Memory viewer roles and quarantine: `memory.py:51-52`.
- Ticket approver roles: `tickets.py:88-95`. Crew names: `seat_events.py:97`.
- Pilot slot set: `roster.py:333` (the `pilot_bootstrap` path stays in Sniper or reads the set from config).
- Host layout: bridge directory, state directory, OS user, unit prefix (`seat_sandbox.py:4-5,45-56`).
- Store tables: `store.py` keeps the generic tables (`agents`, `turn_runs`, `action_requests`, `messages`,
  `tickets`, `heartbeats`, `spend`, `epochs`, `receipts`, `claims`, `decisions`, `grades`, `org_changes`,
  `sealed_views`, `memory_*`); a plug-in declares extra tables (`opportunity_*`, `outcome_captures`,
  `fee_attestations`, `cost_allocations`, `trial_registry`). Existing `office.db` files keep working because
  table names and column definitions do not change.
- Receipt cut: `slot_cut` and `network_id` (`broker.py:50-65`, `capabilities.py:50-58`) become an opaque
  `domain_cut` dict that the plug-in validates. This is the one core schema change with a compatibility cost; it
  can be deferred by keeping the two fields as optional, unnamed-in-docs extras.

### 3c. Model transports

`models.py:46-56` picks adapters by importing Sniper modules. The package ships its own Claude adapter (Agent SDK,
already a dependency) and Codex adapter built from the copied helpers in 2d, and accepts an injected adapter for
anything else.

### 3d. Check that keeps the repository clean

One test in the new repository greps its own tree for a short banned-word list (`sniper`, `memecoin`, `wallet`,
`mint`, `solana`, `pump`, `polymarket`, and the crew role names) and fails on a hit. Cheap, and it enforces the
"no trading, no project names" rule mechanically.

## 4. Keeping Sniper working

Constraints: 28 Sniper modules and 286 test files import `sniper.office.*`; the published tree under
`/srv/sniper-office/release` imports it too; `office.db` holds the experiment's records.

1. **Copy first, do not move.** Stages 1 and 2 only add files to the new repository. Sniper is untouched and
   keeps its own copy. Nothing can break.
2. **Switch by shim, one module at a time (stage 3).** Each extracted `sniper/office/<name>.py` becomes a
   re-export of the package module (`from <package>.<name> import *` plus the private names its callers use).
   Import paths, tests, routes and the pm_desk imports of `egress_proxy`, `netguard`, `clock` do not change.
   Sniper's own test suite is the gate for each switch.
3. **How Sniper consumes it.** A pinned dependency: `pip install` from the local sibling path at a pinned commit
   (or a git URL once a remote exists), listed in `requirements.txt` and added to `seat_publish.SEAT_PINS` so
   `publish_runtime` installs it into `/srv/sniper-office/runtime` for the sandboxed children. `seat_child.py:8-9`
   then imports the package from the runtime, not from `/opt/release`.
4. **No data migration.** Database paths stay in Sniper's seat config; table names stay. The `office` OS user,
   `/srv/sniper-office` and `/var/lib/sniper-office` stay as Sniper's configured values.
5. **Do the switch while the Office is paused.** On this machine it has been since 2026-11-01. Publishing already
   refuses while seats run (`seat_publish.py:81`). If the Office is resumed first, each switch needs a seat
   restart, which is what the unit file already documents (`deploy/office/office-seat@.service`, line 2).
6. **Rollback** is reverting the shim commit in Sniper; the old module body comes back from git.
7. **Other agents work in the Sniper checkout.** Stage 3 touches about 55 files there and should be one crew
   member's task on its own branch, not a side session.

## 5. Staged plan

| Stage | Content | Size | Touches Sniper |
|---|---|---|---|
| 1. Durable core | `store`, `supervisor`, `broker`, `capabilities`, `tickets`, `work`, `memory`, `memory_store`, `clock`, `loops`, `launcher_diagnostics` (imported by `supervisor`); role and table config per 3b; their tests; banned-word test; README with one runnable example (two fake agents, one turn each, a receipt, a killed turn that reconciles) | 1,751 lines of code copied, about 150 changed; about 15 test files | No |
| 2. Seat runtime | Modules of 1b; split `seat_tools.py` and `seat_host.py` along 3a; plug-in loader; unit templates; setup doc for the OS user and bridge directory; Claude adapter | about 4,000 lines copied, about 800 rewritten in the two composition modules; about 25 test files | No |
| 3. Sniper switches over | Dependency pin; shims for about 30 modules; `sniper/office_plugin.py` registering the trading tools, data sources, wakes, notifier; `scripts/office_seat_host.py` reduced to config plus plug-in; publish change | about 600 new lines in Sniper, about 6,000 deleted | Yes |
| 4. v1 launcher and auth (optional) | `child_launcher`, `launcher_*`, `model_child`, `models`, `model_auth`, `host_auth`, `codex_auth`, `scheduler`, `org`, `roster`, `shifts`; copied Codex helpers | about 2,400 lines | Later shims |
| 5. Evaluation interface (optional) | A generic ledger protocol, grader hooks, sealed looks, promotion gate | Unknown; design first | Yes |

**Smallest useful first version: stage 1.** It is the part with zero outside imports today, it is the part the
brief names first (turns, supervisor, leases, capabilities, receipts, memory), and it can be tested without root,
a dedicated OS user, `bwrap` or a model subscription.

**Left out of the first version:** sandboxes and seats (stage 2), model calls, budgets, org and epochs, the web
UI, every unit file, evaluation and promotion, and all of section 1d.

## 6. Decisions for the user

| # | Decision | Recommendation |
|---|---|---|
| 1 | What "the Office" means: durable core (v1), seat runtime (v2), or both | Both, as two layers of one package; durable core first. |
| 2 | Whether the pm_desk seat runtime (`sniper/research/pm_desk/seat_*.py`) is in scope | Out of scope now. Decide after stage 2 whether pm_desk should move onto the extracted seat runtime; today it shares only the egress proxy and clock. |
| 3 | Evaluation and promotion in the first versions | Out. They are trading code today (1c). Keep epochs, sealed views and the separate evaluation file in core; design a generic interface only when a second project needs it. |
| 4 | Repository and package name | A neutral name that is not just `office` (too common as an import). Suggestion: repository `agent-office`, package `agent_office`. |
| 5 | Copy first or move straight away | Copy first (stages 1-2), switch Sniper by shims in stage 3. |
| 6 | Is the Office being resumed, and is it live on another host | Needs your answer. If it stays paused, stage 3 is low risk and can go any time; if it resumes first, stage 3 needs a planned seat restart. |
| 7 | Keep the v1 one-shot launcher (stage 4) | Skip unless you still want v1 as a rollback path; v2 replaced it for running seats. |
| 8 | Isolation as a hard requirement (Linux, `bwrap`, systemd user units, dedicated OS user, root setup once) | Keep it. It is the main thing the runtime offers; a portable mode would be a second code path. |
| 9 | Brain and crew bridge | Optional plug-ins that call project-brain and crewlink; the Office depends on neither. |
| 10 | Web UI | Stays in Sniper. No generic UI in this extraction. |
| 11 | Git history | Fresh repository, with the source commit (`f07f79614`) recorded in the README, as with crewlink and project-brain. |
| 12 | `pm_desk_*.py`, `pm_admission.py`, `intel_*.py` sitting in `sniper/office/` | Leave in place for now; moving them into `sniper/research/pm_desk/` is a separate Sniper clean-up that would shrink `sniper/office/` by 3,149 lines. |

## 7. Decisions taken (user, 2026-11-25) and the revised plan

| # | Decision | Answer |
|---|---|---|
| 1 | Scope | Both layers: durable core and seat runtime. |
| 2 | pm_desk seat runtime | In scope. It converges onto the extracted seat runtime. |
| 3 | Evaluation and promotion | In. A generic interface is designed, not left to a later project. |
| 4 | Name | Repository `Sniper-Office`, package `sniper_office`. |
| 5 | Copy or move | Move. Sniper ends up importing the package; its own copies are deleted. |
| 6 | Resume | Not resuming now. The Office must stay ready to resume at any time. |
| 7 | v1 one-shot launcher | Keep or improve. |
| 8 | Isolation | Left to the builder: keep Linux, `bwrap`, systemd user units and a dedicated OS user as a hard requirement. |
| 9 | Brain and crew bridge | Optional plug-ins; the Office must be usable for any local project. |
| 10 | Web UI | A separate UI that ships with the Office, not a tab inside Sniper's dashboard. |
| 11 | Git history | Fresh. |

Revised stages (replaces the table in section 5):

| Stage | Content | State |
|---|---|---|
| 1 | Durable core, 12 modules | Done, `Sniper-Office` commit `47a3dfd`. |
| 2a | Seat runtime modules with no domain coupling: egress, sandbox, events, budgets, retention, in-sandbox children | Done, commits `b175cb5`, `b9787ab`. |
| 2b | Tool server and seat host rebuilt on a plug-in loader (section 3a); database tool; publish step; unit template; setup guide | Done. End-to-end run on this host, 2026-11-25, from a separate test bridge under `/var/tmp` (no root, access list for the sandbox user; the live bridge was only read, for its Python runtime): package published; seat host prepared the seat and started both proxies; tool calls made through the real socket as the sandbox user ran a shell in the sandbox, sent mail to the operator and set a sleep; from inside the sandbox a public site answered 200 through the proxy, a denied host, the host's loopback, direct internet, a write outside the workspace and a read of the host home all failed; a peer that is not the sandbox user got nothing; every call was recorded. **Not run: the model call.** The Claude credential file on this host had expired 45 hours earlier, the seat does not refresh tokens, and the host parked with `claude_auth_expired`, which is the designed behaviour. |
| 3 | v1 launcher, auth snapshots, org, roster, shifts, scheduler (decision 7) | Done in code and tests. Left for stage 4: evaluated epochs, nominee checks, the one-use launcher policy. Not extracted: the watchdog daemon that writes the guard file, and unit files for supervisor and launcher (Sniper's are tied to its paths). |
| 4 | Evaluation and promotion interface (decision 3) | Built except the optional statistics extra, a grader entry point and the auditor: see `docs/EVALUATION.md` section 11. |
| 5 | Stand-alone web UI (decision 10) | Built: overview, seat, mail, requests, budget, evaluation, health, project panels, two actions. Not built: the optional illustrated floor. See `docs/UI.md`. |
| 6 | Move (decision 5): Sniper depends on the package, `sniper/office/` core modules become re-exports and then go, trading tools register through `sniper/office_plugin.py`. Touches Sniper, so it runs on its own branch there and only on the user's word. | After stages 2-3. |
| 7 | pm_desk seats move onto the seat runtime (decision 2) | After stage 6. |

"Ready to resume" (decision 6) is the gate for stage 6: before Sniper's copies are deleted, a seat must wake end to
end from the package on this host (publish, sandbox, one tool call, one mail), with the trading plug-in loaded.
