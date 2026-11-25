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
| `desk_db` | feed_list, sheet_read, paper_order | `data/memecoin_desk.db` |
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

## What each tool is here

- `results_view`, `book_view`, `candidate_register`, `feed_filter_set`, `book_halt`: real, against the ledger files (read-only) and the seat's own office db (tables `desk_*`). `book_view` equity is cash for a flat book; with positions open it is cash plus each position's newest mark (modelled net exit value) when every mark is fresher than `mark_max_age_s` (120 s); otherwise `unknown`, `position_unpriced`, cash shown. The source project's old ledgers carry no USD marks, so their open positions stay `unknown`.
- `feed_list`, `sheet_read`: the stored hard-filter passes and their stored evidence, state `partial`. The near-pass feed and the live sheet builder stay in the source project. A desk db whose newest ticket is older than `feed_stale_s` is `unknown`, not an empty feed.
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

`floor/` is the source project's rooms page with this office's seats. `floor/rooms.json` maps seats to rooms (a role not listed sits in Research):

| room | seats |
|---|---|
| Newsroom | narrative |
| Research | scout, researcher, forensics |
| Debate | judge, judge_gpt |
| Decision | head |
| Trading | none: the paper book |
| Situation | none: a world map |

Unknown or empty on purpose, because this store holds nothing for them: prediction strategies, perps and stock margin returns, the scanner, headlines, world-map stories and the paper charts. Those header stats are replaced by this office's own (seats awake, wakes, tokens, mail, requests, tool calls, paper book). The map draws no points. The source page's other rooms (Strategy Lab, Paper Labs, Explorer, Memes, Sports, Video rule lab, Coach, Control) are not ported. The Trading book is `unknown` unless `--ledger-db` is given: it counts open and closed orders, wins and net of costs for closed orders with a recorded exit price (modelled fills, no landing proof).
