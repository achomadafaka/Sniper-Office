# The desk plug-in

`sniper_office.desk` adds the paper-book, chain, wallet-network and brain tools of the source project to a seat. Stdlib only, no import from the source project. Paper only: no signing key, no live path, no runner gating.

```
"plugins": ["sniper_office.desk"],
"tool_groups": ["paper", "chain", "network", "brain"]      # per seat, see below
```

A tool whose config key is missing, or whose file cannot be read, answers `{"state":"unknown","reason":...}` as an error. Nothing is defaulted.

## Config keys (all optional, per seat)

| key | used by | source project path |
|---|---|---|
| `desk_db` | feed_list, sheet_read, paper_order | the Office desk db the feed collector writes (`~/.local/share/sniper-office/desk.db`, see below); a source-project db of the same shape also works |
| `ledger_db` | book_view, paper_order, paper_exit, results_view | the Office ledger the filler writes (`~/.local/share/sniper-office/ledger.db`); one file, one book per judge |
| `results_ledgers` | results_view (list; default `[ledger_db]`) | the Office ledger, optionally followed by the source project's old ledgers as read-only history |
| `ledger_epoch` | book id in the ledger (default: the seat epoch) | `office-v2-explore-gpt-1` for the GPT judge |
| `book_policy` | book_view, paper_order rail bounds | `config/office/book_policy_pilot.json` |
| `clock_file` | feed_list, book_view, candidate_register | `data/clock_offset.json` |
| `research_env_file` | chain_*, network_expand (needs `MEMECOIN_RESEARCH_HELIUS_RPC_URL`, https) | the research env file |
| `chain_db` | chain cache and pace (default: beside `office_db`) | |
| `wallet_network_db` | network_read, network_expand | `data/office/wallet_network.db` |
| `brain_dir`, `brain_package`, `brain_python` | brain_read, brain_note | folder holding the Office's `.brain/` (this repo), the project-brain checkout, optional python |
| `feed_window_s`, `feed_stale_s` | feed_list (defaults 3600, 900) | |
| `sheet_live` | sheet_read: false skips the live pool re-read and the 60 s trades (default true) | |
| `ticket_wakes`, `ticket_wake_min_s` | wake source: true to be woken for new feed passes, at most one wake per `ticket_wake_min_s` (default 300); never for researcher or narrative | |
| `alert_loss_pct`, `alert_gain_pct`, `alert_trail_arm_pct`, `alert_mark_max_age_s`, `wake_lookback_s` | position alerts (15, 50, 20, 300) and how far back a closed order may still wake (86400) | |

Feed collector config (`desk_feed.json`) adds `min_prints_m5` (default 2): a pair with fewer m5 trades has an unknown chart check, so SKIP.

## What each tool is here

- `results_view`, `book_view`, `candidate_register`, `feed_filter_set`, `book_halt`: real, against the ledger files (read-only) and the seat's own office db (tables `desk_*`). `book_view` equity is cash for a flat book; with positions open it is cash plus each position's newest mark (modelled net exit value) when every mark is fresher than `mark_max_age_s` (120 s); otherwise `unknown`, `position_unpriced`, cash shown. The source project's old ledgers carry no USD marks, so their open positions stay `unknown`.
- `feed_list`: the stored hard-filter passes, state `partial`. Freshness is the collector's (newest ticket of any verdict, or newest `feed_seen`), not the passes': a live collector with no pass in the window answers `known`, empty, with `collector` (heartbeat age, PASS/SKIP counts, top SKIP reasons); a collector older than `feed_stale_s` is `unknown`.
- `sheet_read`: tickets carry `evidence.market` (DexScreener run-up m5..h24, volume, txns, reserves, liquidity, age, fdv/mcap at decision time). `sheet_read` shows it apart from `now_read` (a fresh DexScreener read with its own fetch time) and `trades_60s` (GeckoTerminal recent trades, with coverage). `known` when all of that is complete, else `partial` with a `missing` list. cluster and dump_group stay unknown (not collected here). Provider claims, not fills.
- `market_data` (group `market`): the same DexScreener read for a mint or pool, host side, labelled not a fill price.
- Wake sources (`wakes.py`): `new_ticket` (rate limited, once per ticket per seat, honours the seat's feed filter), `order_closed` (the book's judge, once per order) and `position_alert` (urgent; loss, gain, trail armed, once per order and trigger). Once-only state is `desk_wakes` in the seat's office db.
- `paper_order`, `paper_exit`: checked (role, plan, rails, halt, one entry per coin, order open in the book), then recorded in `desk_paper_requests` with state `queued`, `filled: false`. `filler.py` fills them (below).
- `chain_*`, `network_*`: the source project's read-only chain data and wallet network, ported as `chain_data.py` and `wallet_network.py`.
- `brain_*`: the Office's own project brain (`.brain/` in this repo, started with `brain init`). `brain_read` runs `brain ask`. `brain_note` records the text as a `note` episode, then writes an unverified `observation` fact on `actor:<role>` citing it. The seat's `role` is its brain role.

## The fill engine

`python3 -m sniper_office.desk.filler --config filler.json` (unit: `deploy/office-paper-filler.service`, config: `config/paper_filler.example.json`) is a host process, not a seat. Each tick it reads `desk_paper_requests`, and in its own ledger (`ledger_db`) it fills entries, marks open positions, applies sl / tp / trail / catastrophe / max-hold rails and executes exits and exit requests. One ledger file holds one book per judge (`books[].actor_ids` routes a request to its book; `book_id` is the seat's `ledger_epoch`). Paper only: no key, no signing, no chain access.

- Price source: the public DexScreener pair lookup, a provider claim with no timestamp of its own. Each fill, mark and exit records price, liquidity, pair, source, and our fetch time (`orders.fill_proof`, `orders.exit_proof`, `marks`). It is not a proven landing price; the source project filled from its own committed PumpSwap tape, which is not available here.
- Fill model (`desk-paper-v1`): entry price = quote * (1 + slippage), slippage = `slippage_bps` + 2 * clip / liquidity (constant-product estimate); fee = `fee_bps` of the clip; an entry whose impact exceeds `max_impact_bps` does not fill. Exits mirror it. Values come from `book_policy` (`clip_usd`, `max_concurrent`, `daily_loss_usd`, `sl_pct`, `tp_pct`, `trail_pct`, `catastrophe_pct`, `max_hold_s`, `entry_timeout_s`, `slippage_bps`, `fee_bps`, `max_impact_bps`, `mark_max_age_s`); a key it lacks falls back to the default in `filler.DEFAULTS` and the order's frozen `policy_json` lists it under `defaulted`.
- Unknown price, liquidity or clock: no fill and no mark value. An entry retries each tick until `entry_timeout_s`, then is closed as `censored` with the last reason (`request_outcomes`, `fill_attempts`). An open position with no price stays open, unpriced, and is retried; it is never closed at an invented price.
- At-most-once: order_id is the request id; the outcome row and the rows it causes are one ledger transaction, so a restart skips what is done.
- Not modelled: the fixed network and priority cost, entry/exit landing delay (a fill is priced at the fetch that triggered it, optimistic), trigger-to-exit slippage on a fast move.


## The seat floor page

`floor/` is the source project's rooms page, whole, filled with this office's seats and paper books (what each room shows and where it comes from is in `docs/UI.md` section 10). `floor/rooms.json` maps seats to rooms (a role not listed sits in Research; a seat may sit in several rooms), `floor/sources.json` holds the page's read-only SQL.

| room | seats |
|---|---|
| Newsroom | scout |
| Research | researcher |
| Explorer | forensics |
| Memes | narrative |
| Debate | judge, judge_gpt |
| Decision | head |
| Trading | judge, judge_gpt |
| Control, Situation, Strategy Lab, Paper Labs, Sports, Video rule lab, Coach and skills | none |

What each room is filled from:

| room | seats | filled from |
|---|---|---|
| Newsroom | scout | feed tickets (the collector's database, when present), candidate coins, seat thoughts, mail |
| Research | researcher | seat requests (as proposals), pages read, notebooks, tools used |
| Explorer | forensics | notebooks, requests, pages read |
| Memes | narrative | seat thoughts, paper hit rate across the ledgers |
| Debate | judge, judge_gpt | mail, standups, tool calls, wakes |
| Decision | head | open and closed orders, requests, tasks, halts |
| Trading | judge, judge_gpt | one book per ledger: summary, charts from marks, orders, requests to the filler, outcomes, capital |
| Control | none | seats, wakes, tools, standups, mail, requests, health, operator chat |
| Situation, Strategy Lab, Paper Labs, Sports, Video rule lab, Coach and skills | none | nothing here holds data for them: the rooms and their tabs stay and say unknown |

Unknown or empty on purpose, because this store holds nothing for them: prediction strategies, perps and stock margin returns, the scanner, headlines (the feed tickets stand in when the feed collector's database exists), world-map stories, the video rule lab, sports, the coach. The Trading books are one per `--ledger-db`: open and closed orders, wins and net of costs for closed orders with a recorded exit price (modelled fills, no landing proof), and a line chart per order from the ledger's marks.

## Live feed collector

`python3 -m sniper_office.desk.feed --config feed.json [--once]` (unit: `deploy/office-desk-feed.service`, config: `config/desk_feed.example.json`) is a host process, not a seat. It writes one `desk_tickets` row (stage `hard_filter`) per pool into the Office desk db, so `feed_list`, `sheet_read` and `paper_order` work with no other process. Point seats' `desk_db` at its `desk_db`.

- Sources (https only, fixed host allowlist, no redirects, 2 MB cap, per-host pause, 429 backoff): DexScreener token profiles, token boosts and token pairs; GeckoTerminal new pools; Jupiter lite quote (sell round trip); optional research RPC (`research_env_file`, key `MEMECOIN_RESEARCH_HELIUS_RPC_URL`; methods `getAccountInfo`, `getMultipleAccounts`, `getTokenLargestAccounts`, `getTokenSupply` only; url never logged or stored).
- Each check row: `status` (pass / kill / unknown), `reason`, `source`, `fetched_at`, the values, and for market data the provider's claim. Nothing is invented; unknown is recorded as unknown.
- Verdict (ported from the source hard filter): any kill => SKIP; unknown market_data, token_safety, sell_probe, liquidity, mcap, chart or holders => SKIP; else PASS. Unknown cluster / dump_group never refuse. Kills: `liq_min_quote_sol` (default 10 SOL quote side, source tests used 1 to 40), `mcap_safety` [5000, 50M] USD, mint or freeze authority open, transfer hook, round trip loss above `round_trip_loss_pct_max` (8), 20+ buys and 0 sells in 5 min, pair older than `max_pair_age_s` (Office addition).
- Dropped, needs the source's private tape or graph: dump_group, cluster, mayhem flag, 60 s chart prints, pool-math sell probe (replaced by a Jupiter route round trip at `probe_sol`). Without the RPC key, token_safety and holders are unknown, so nothing PASSes.
- A mint the provider cannot price yet is retried each cycle until `retry_unknown_s`, then written SKIP unknown. One ticket per mint (its best-liquidity pair). Retention: `retention_days` and `max_rows`, applied each cycle.
