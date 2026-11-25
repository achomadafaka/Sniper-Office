# The services plug-in

`sniper_office.services` ports the research and host services the source project's Office relied on. Stdlib only, no import from the source project, paper and read-only: no signing key, no order path. Tools are opt-in per seat (see `config/services_seat.example.json`):

```
"plugins": ["sniper_office.services"], "tool_groups": ["wallets", "creator", "narrative"]
```

A tool whose config key is missing or whose file cannot be read answers `{"state":"unknown","reason":...}` as an error.

| host process | unit | config | writes | read by tool |
|---|---|---|---|---|
| `services.host_watchdog` | `office-host-watchdog` | `host_watchdog.example.json` | the guard file (`guard_file`) | none: `host_guard.external_guard` and the supervisor read it |
| `services.xcap` | `office-xcap` | `x_receiver.example.json` | `x_db` | `x_mentions` |
| `services.narrative` | `office-narrative` | `narrative.example.json` | `narrative_db` | `narrative_matches`; the seat writes guesses with `narrative_guess` |
| `services.creator.main collect` | `office-creator-collect` | `creator_collect.example.json` | `state_dir` (checkpoint, discovery files) | `creator_status`, `creator_launches` |
| `services.creator.main venue` | `office-creator-venue` | `creator_venue.example.json` | `state_dir` | same, `which="venue"` |
| (none, on demand) | | | `wallet_db` | `wallet_analyze`, `wallet_findings` |

`services.list_claims` is a library (pure parser of provider list claims); nothing runs it.

## Host watchdog

Replaces the source's host monitor, resource watchdog and supervisor wake timer. Each second it reads CPU and IO pressure, MemAvailable, the age of the heartbeat files in `heartbeat_files` and, if `office_cgroup` is set, the cgroup caps, and writes the guard file `host_guard.external_guard()` expects. A pressure or heartbeat pause lifts after `resume_after_s` (900) of continuous health; a pause for caps or lost telemetry stays until the operator restarts it with `--initialize`. `kill_file` (an `office.slice/cgroup.kill`) is off by default; set it to have a new pause kill the slice, as the source did. Point the supervisor's `--guard` and the launcher at `guard_file`. Not ported: the funded-runner heartbeat check (the source's reason for the pause), the pinned pilot manifest hash, and the wake timer (the supervisor already pauses on an unready guard and resumes by itself).

## X capture

`services.xcap` listens on loopback only. The userscript in the operator's own browser POSTs `{"run_id","status","cards":[html,...]}` to `/capture` with header `X-Office-Token`. Cards are sanitized, parsed (direct, repost, quote, reply), normalized (a coin counts only with a chain from the post's own content; a bare ticker is ambiguous) and stored. With no fresh clock sample a capture is stored as `legacy_unknown` and left out of reads. The source's tier lists of handles are not shipped: put an allowlist in `handles`, or leave it empty.

## Wallet analyzer

A screen of a prediction-market wallet (Polymarket on Polygon, the one chain the source supports), from public closed positions, open positions and trade activity: FIFO flips, flip rate, held or flipper or mixed mode (0.30 / 0.70), a per-branch ROI interval, a verdict (`edge_detected`, `no_edge`, `underpowered`, `operator_review`, `data_insufficient`), findings with deterministic uids, per-tag flipper grain, supersession of older runs. It is a screen, not the source's protocol: the capital ledger, timing, sizing, liquidity, exit attribution and bootstrap surfaces were not ported (they need the source's duckdb ledger and about 23k lines). The Polymarket data API did not answer from the development host, so the fetch layer is tested against fakes only.

## Narrative

The source labelled launches from a Solana gRPC stream (Yellowstone, `MEMECOIN_SHYFT_GRPC_TOKEN` and `MEMECOIN_SHYFT_GRPC_URLS`, needs `grpcio` and generated stubs) run inside the funded runner. That stream is not ported. The labeller here polls `launch_sources`: GeckoTerminal new pools (first sighting = our fetch time) or any `launches` table with `mint,name,symbol,received_at` (what a launch listener in `sniper_office/desk` can write). The minimal gRPC-free option for a faster feed is a Solana `logsSubscribe` websocket on the create program, writing that table. The narrative seat writes its own guesses (`narrative_guess`: one generation per 10-minute slot, immutable); a launch matches a guess only if the guess was written strictly before the launch was first seen; no fresh generation means unknown, never none.

## Creator research

Ported with their modules from the frozen collector releases. Both read a BSC public RPC (`eth_chainId`, `eth_blockNumber`, `eth_getBlockByNumber`, `eth_getLogs`, `eth_call`, `eth_getBlockReceipts`, `eth_getTransactionByBlockHashAndIndex`) at most one call per second and keep their own state directory. The endpoint URL lives in an env file named by `env_file`; its variable name is `rpc_url_env`; the URL is never printed, and it must not equal or share an account with any URL in `other_env_files`. `eth_abi` and `eth_utils` are replaced by `creator/_abi.py` (Keccak-256, uint256 / address / string / bytes / tuples), checked against both on the real seed. The old runs stopped on 2026-10-20 (collect `blocked: missing_result`, 0 launches admitted, venue watch with no observations); their anchor is a month behind head, so do not resume it: write a new anchor (`creator.main --config c.json anchor`) and start a fresh state directory. A free public node rate-limits `eth_getBlockByNumber` (the collector backs off, correctly); use a research endpoint.

## Wiring left for the desk plug-in

See the hand-off in the commit message and the task report: list claims into `sheet_read`, X mentions into the sheet, the narrative launch table from the J7 listener, the heartbeat file from the seat host.
