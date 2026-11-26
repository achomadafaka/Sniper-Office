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
- `brain_*`: the Office's own project brain (`.brain/` in this repo, started with `brain init`). `brain_read` runs `brain ask` (or `brain entity` when given a `topic`, an entity id such as `lane:perps`); an optional `kind` filters client-side; exit 0 with no facts is `state known, facts 0`, never an outage. `brain_note` records the text as a `note` episode, then writes a fact citing it on `actor:<role>` plus up to 6 `topics` (`type:name`, validated; the entity types are in `.brain/config.json`). `kind` is observation (default, unverified), lesson (stored as a finding), rule, trap, finding, measurement or decision; rule, trap and lesson need a topic and 40+ chars; statements are 600 chars at most. Non-observation kinds cite the note episode as `episode:` evidence. Links between facts are not offered: the brain CLI has no link option. The seat's `role` is its brain role. The host also writes to the brain, with the same keys (`brain_boot_bytes`, default 6000, bounds the boot text): each completed handover is stored as a `handover` episode only (a log, no fact); each closed coin order of a judge seat and each closed lane position (`book_lanes`) leaves a `report` episode and a `finding` fact (once per order or position; topics `lane:<lane>`, the mint, coin, market or stock, and `strategy:<id>`); and the first wake of a new session is shown `brain boot <role>`. A brain that is down is skipped, never blocks a handover or a wake. If a note is too close to an existing fact (the brain asks for a judgement), `brain_note` keeps it as an episode and says so.

## The fill engine

`python3 -m sniper_office.desk.filler --config filler.json` (unit: `deploy/office-paper-filler.service`, config: `config/paper_filler.example.json`) is a host process, not a seat. Each tick it reads `desk_paper_requests`, and in its own ledger (`ledger_db`) it fills entries, marks open positions, applies sl / tp / trail / catastrophe / max-hold rails and executes exits and exit requests. One ledger file holds one book per judge (`books[].actor_ids` routes a request to its book; `book_id` is the seat's `ledger_epoch`). Paper only: no key, no signing, no chain access.

- Price source: the public DexScreener pair lookup, a provider claim with no timestamp of its own. Each fill, mark and exit records price, liquidity, pair, source, and our fetch time (`orders.fill_proof`, `orders.exit_proof`, `marks`). It is not a proven landing price; the source project filled from its own committed PumpSwap tape, which is not available here.
- Fill model (`desk-paper-v1`): entry price = quote * (1 + slippage), slippage = `slippage_bps` + 2 * clip / liquidity (constant-product estimate); fee = `fee_bps` of the clip; an entry whose impact exceeds `max_impact_bps` does not fill. Exits mirror it. Values come from `book_policy` (`clip_usd`, `max_concurrent`, `daily_loss_usd`, `sl_pct`, `tp_pct`, `trail_pct`, `catastrophe_pct`, `max_hold_s`, `entry_timeout_s`, `slippage_bps`, `fee_bps`, `max_impact_bps`, `mark_max_age_s`); a key it lacks falls back to the default in `filler.DEFAULTS` and the order's frozen `policy_json` lists it under `defaulted`.
- Unknown price, liquidity or clock: no fill and no mark value. An entry retries each tick until `entry_timeout_s`, then is closed as `censored` with the last reason (`request_outcomes`, `fill_attempts`). An open position with no price stays open, unpriced, and is retried; it is never closed at an invented price.
- At-most-once: order_id is the request id; the outcome row and the rows it causes are one ledger transaction, so a restart skips what is done.
- Not modelled: the fixed network and priority cost, entry/exit landing delay (a fill is priced at the fetch that triggered it, optimistic), trigger-to-exit slippage on a fast move.


## The J7 copy lane (paper, any chain)

A second lane beside the feed collector: follow selected pro traders through the J7 relay of fomo, and let one seat (`copy_desk`, own $1000 book) decide whether to copy. Paper only.

```
j7 listener (desk/j7.py) --> j7.db: j7_signals + desk_tickets --> copy_desk seat (signal wake, urgent)
tick recorder (desk/ticks.py) --> j7.db: ticks, office.db: desk_live_pnl
paper filler (existing, ticks_db set) --> ledger.db book office-v2-copy-1
```

- Listener: `python3 -m sniper_office.desk.j7 --config j7.json [--once --duration 120 --no-refresh]` (unit `deploy/office-j7-listener.service`, config `config/j7_listener.example.json`). A stdlib Socket.IO client (`ws.py`) on J7's wallet-tracker service, one connection per account in the J7 env file, reconnect with backoff (5 to 120 s), access token renewed an hour before expiry through the refresh cookie pair (the rotated pair is written back atomically under a lock; `--no-refresh` or `"refresh": false` never rotates). An expired token with no refresh token is `token_expired` in the status, not a retry storm.
- Filter (`decide`): trader on the roster (`traders_file`, matched by J7 user id or handle; `accept_unlisted` lets everything J7 relays through), chain followed, fresh (`max_age_s` 300), size floor `min_usd` per chain (default $200, the source project's absolute source-buy floor; per-trader `min_usd` overrides, as its per-wallet overrides did; optional `min_mcap_ratio`, the source project's 0.5% rule, off here). Thesis posts need a roster trader and text (`thesis_min_holding_usd`). A sell is stored without a ticket or wake.
- Stored once (`signal_id` is J7's event id, `INSERT OR IGNORE`; ticket in the same transaction): trader, chain (DexScreener chainId; unknown J7 chains fall back to J7's own network name), token, side, USD size, price, token qty (usd / price), mcap, J7's receipt time, trade time, our receive time, tx ref. J7 relays no native amount (`native_amount` NULL) and no Solana signature (`tx_ref` = J7 event id; an EVM id embeds the tx hash, parsed out). Tickets carry no market snapshot, so `sheet_read` is `partial` by design; its live re-read and `ticks_120s` are the facts.
- Roster seed `config/j7_traders.example.json`: the source project's J7 tracked lists (fomo accounts 0 and 1, pump account 0, as of 2026-11-23), tier `armed` for its five `real_approved` fomo wallets.
- Chains: every DexScreener chain. `chains.py` maps J7 ids (Solana, BSC 56, Base 8453, Ethereum, 4663, Arbitrum, Polygon, Optimism, Avalanche, Sonic) to chainIds. `sheet_read`, `market_data(chain=)` and the filler's price lookup are chain aware; trade counts need GeckoTerminal and are unknown on chains it lacks. An unknown price or liquidity is still no fill.
- Ticks: `python3 -m sniper_office.desk.ticks --config ticks.json` (unit `deploy/office-j7-ticks.service`, config `config/j7_ticks.example.json`). Each second, one batched DexScreener pair lookup per chain (30 pairs max) for open positions first, then signals inside `watch_after_signal_s` (default 600 s, from our receive time; nothing earlier exists). At most 4 requests a second. **Achieved resolution** (measured live 2026-11-25, Solana, BSC, Base, Ethereum): the poll gap median is 0.99 s (p90 1.0 to 1.15 s, worst 2.4 s), but the provider's price changes about every 30 s (gap median 30.3 s on SOL, BSC and ETH pairs, 470 txns per 5 min on the SOL pair), so the real price resolution is about 30 s on every chain. GeckoTerminal trades lagged about 100 s and rate-limit at 30 a minute; the on-chain pool read below replaces the provider as the primary price. `resolution()` (and `chart_read`) report both gaps from the stored rows. A failed poll leaves a gap, never a carried price.
- **On-chain prices** (`desk/onchain.py`, primary source; DexScreener is the fallback). A coin's DexScreener pair is decoded once from its pool's own state, then followed: Solana by websocket `accountSubscribe` (a push on every change) on the research Helius URL (`research_env_file`, key `MEMECOIN_RESEARCH_HELIUS_RPC_URL`; read-only `getMultipleAccounts` for the seed and after a reconnect, wss on the same host), EVM by one Multicall3 `aggregate3` per chain per `evm_poll_s` (block number and block timestamp read in the same call) over public RPCs (`evm_rpc`, defaults publicnode). Supported: Solana PumpSwap, Raydium AMM v4 / CPMM (vault minus protocol and fund fees) / CLMM, PancakeSwap Solana CLMM, Orca Whirlpool, Meteora DLMM, pump.fun bonding curve; EVM Uniswap v2-style (`getReserves`) and v3-style (`slot0`, also PancakeSwap v3, Aerodrome Slipstream). Not supported (stay on DexScreener): Meteora DAMM v1/v2 and DBC, Uniswap v4 (pool ids, no pair address), Balancer-style and anything else. A pool whose decoded price is more than `max_dev` (60 %) off the provider's at resolve time is refused. **USD**: the pool gives the watched token in units of its other token; that token's USD price is the provider's `priceUsd / priceNative` for the same pair, refreshed every `ref_s` (30 s, one batched request, recorded in `onchain_ref` beside the on-chain price for a cross-check); liquidity in a tick is the provider's from the same refresh. A tick row is written when the price moves (checked every `flush_s`, 0.25 s) and a heartbeat row every `heartbeat_s` (1 s) while the feed is alive, so the filler's newest-tick lookup works on a quiet pool. `ticks.source` is `onchain:<pool kind>` or `dexscreener:pairs_batch`; `slot` is the Solana slot or EVM block, `seen_at` our receipt time of that state, `chain_ts` the EVM block timestamp (Solana carries no block time in the push). A feed that is down (ws closed, RPC failing) writes nothing and the same cycle polls the provider again. Commitment is `confirmed` (`commitment: processed` is about a slot faster). The filler's marks and exits carry the tick's `source`.
- **Exit price path** (port of the price half of the source project's record-only exit observer, `sniper-desk-exit-observer.service` -> `memecoin_desk_risk_shadow.py --exit-observer` -> `memecoin_desk_exit_observer.py`): for every open position and every cycle the recorder writes `exit_path(order_id, at, price_usd, source, slot, net_usd, peak_usd, drawdown_pct, covered_from_fill)` in the J7 db: the price and where it came from, the modelled net exit value (after slippage and fee, as the filler would exit), its running peak since we first saw the position, the drawdown from that peak, and whether the peak covers the fill moment (the observer's `peak_covered_from_fill`). Not ported: the observer's swap-tape indicators (SELL_BURST, EXODUS, STRUCT_BREAK, ONE_SWAP_DROP, TOP_DUMP and CREATOR_SELL proxies), which need every swap's signature, actor and post-swap reserves from a transaction tape; account subscriptions do not carry those.
- Marks, exits, PnL: with `ticks_db` set the filler prices a fill, mark or exit from the newest tick (at most `tick_max_age_s` old, default 5 s) and otherwise does one public lookup; a book may carry `book_policy` and `mark_every_s` (the copy book checks tp/sl every second). `book_view` now shows `pnl` (realized, unrealized, total, initial) and each position's `unrealized_usd`; `position_live` adds the newest tick price and age per position; the recorder writes the same to the office db table `desk_live_pnl` (one row per open position, and a book row with `order_id` empty) every second for the UI. Unrealized is the modelled net exit value minus what the entry cost; an unpriced position is never valued.
- Seat (`role copy_desk`, group `copy`): `signal_list`, `signal_read` (trader hit rate from closed windows: return from first tick after the signal to the end of the window, scored only with 20+ ticks), `chart_read`, `position_live`, plus `paper_order` (market entry, `sl_pct` and `tp_pct` mandatory) and `paper_exit`. Wake `j7_signal` is urgent, once per ticket per seat, ids and numbers only (trader text never enters a prompt). Files: `sniper_office/desk/prompts/copy_desk.md`, `config/seat_copy_desk.example.json`, `config/book_policy_copy.example.json`, `config/paper_filler_copy.example.json`.


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
