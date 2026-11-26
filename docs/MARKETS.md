# The markets plug-in

`sniper_office.markets` adds paper-only Polymarket, perps, stocks and news lanes, a story router, and the data behind the floor's Newsroom, Situation, Strategy Lab, Paper Labs, Sports, Explorer, Coach, Video rule lab and Control rooms. Stdlib only, no import from the source project, no venue key, no order to any venue.

```
"plugins": ["sniper_office.desk", "sniper_office.markets"],      # desk only if the seat also uses its brain tools
"tool_groups": ["polymarket", "perps", "stocks", "news"],        # any of the four, per seat
"markets_db": ".../markets.db", "markets_ledger_db": ".../markets_ledger.db"
```

## What the source project had, and what is here

| lane / room | source project | here |
|---|---|---|
| Polymarket news desk | RSS intake (38 feeds) into `news_items`; a BM25 shortlist of open markets per headline; a debate (scout, bull, bear, narrative, head); a call = buy the token at the CLOB best ask, scored on the bid at 15 m..7 d and at resolution, less a taker fee `rate*p*(1-p)`; strategy registry; forced-pick "news calls" with a control arm | `news.py` (same 35 feeds that answer, no key), `router.py` (overlap with open markets, same idea), debate as `story_debate` turns between `newsdesk`, `pm_trader`, `pm_critic`; `polymarket.py` feed (Gamma + CLOB); `books.py` fills at the fetched ask, exits at the bid, settles at resolution; `strategies.py` (the written strategies, experiments, computed verdicts); `news_call` + `call_scores` |
| Perps | Binance USDT-futures public data, a seat opening leveraged longs/shorts with stop and target (cost 6 bps/side, 4 h max hold, leverage cut so a stop costs at most half the margin; funding and liquidation NOT modelled); Hyperliquid microstructure paper engine | `perps.py` (Hyperliquid info API; Binance as an optional venue), `books.py` with funding accrual, liquidation price, max hold, the same leverage rule |
| Hyperliquid intel service (`sniper-office-intel-hl`) | forward-only hourly BTC candle recorder, 15 s after the hour, a pinned receipt per candle | `intel_hl.py`: same rule (exact bar match, never backfilled, a missed hour is a `missed` row), any coins, own unit |
| Stocks | TradingView screener + Yahoo 1 m bars (no key); day book: regular session, nothing new in the last 30 min, flat 5 min before the close, leverage <= 5, 2 bps/side | `stocks.py` (Yahoo chart for the quote and the session times, the same screener), `books.py` with the same rules |
| Situation (world map) | places from model receipts (scout/mapper) on fixed anchors (`PLACES`) | rule match of country and region names in the headline onto the same anchors; seats add or correct with `story_place` |
| Sports | NBA closing-line study: fair line from a bookmaker, de-vigged, against the market ask | `sports.py`: ESPN scoreboard with its bookmaker line de-vigged, and `edges()` against dated winner and draw contracts plus structured NFL moneylines |
| Strategy Lab | `strategies.book`: per-strategy settled/open, verdict only from 10 settled | `strategies.results` over the Office ledger |
| Explorer | free-hour briefs and lead outcomes | `explorer_note` / `explorer_notes`; leads scored at resolution. Not wallet or chain forensics here |
| Coach and skills | skills registry with history, coach reviews, lessons | `skill_save`, `skills_read`, `review_add`, `lesson_add`, `lessons_read` |
| Video rule lab | caption-derived rules, back-tested on stored NQ bars | `video_rule_add` / `video_rules` store the rules as paper rules under test; the back-tester is not built (needs stored bars) |
| Control | agent map, cleanup, health, jobs, chat | `api.control`: feed and engine health, per-book halt, universe sizes, events, chat, job intervals; `book_halt` and `operator_say` as write actions |

### Not ported, and why

- Algorithm lab: runs seat-written code in a jail; needs a sandbox that is not part of this plug-in.
- Post-count (Musk) lane: needs a post counter source the rules name; none is keyless.
- Auto-fire lane and the $10 forced-trader lane's seat sessions: they run on the source project's rule feed and seat bus. The news-call lab (forced pick, control arm) is the part that transfers.
- X list reads: need an account key. Add `x`-style sources by RSS URL if you have a bridge.
- Model-called routing (scout/mapper/headline_instruments/memes narratives): replaced by rules plus `classifier_hook` (`"package.module:function"`, called as `fn(item, result) -> result`) and by seat judgement (`story_route`, `story_place`).
- Paper hit rate of memes against a launch tape: no launch data is collected here.
- Polymarket was unreachable from the machine this was built on (the host timed out); parsing follows the source project's own Gamma/CLOB reads and is tested with fakes only. Verify with one `--once` on a machine that reaches it.

## Host processes

`python3 -m sniper_office.markets.<name> --config X.json [--once]`. Units: `deploy/office-markets-feed@.service` (instances polymarket, perps, stocks, news, sports, books) and `deploy/office-markets-intel-hl.service`; configs `config/markets_<name>.example.json`. A tick that fails is logged and the loop goes on; each writes a `mk_feed_status` row. https only, no redirects, 2 MB cap, fixed host allowlist for the APIs (`common.API_HOSTS`), named public hosts only for feeds.

| process | source | no-key | writes |
|---|---|---|---|
| polymarket | `gamma-api.polymarket.com/markets`, `clob.polymarket.com/book` | yes | `mk_pm_markets`, `mk_pm_quotes` |
| perps | `api.hyperliquid.xyz/info` metaAndAssetCtxs; optional `fapi.binance.com` | yes | `mk_perp_ticks` |
| intel_hl | `api.hyperliquid.xyz/info` candleSnapshot | yes | `mk_perp_candles` |
| stocks | `query1.finance.yahoo.com/v8/finance/chart`, `scanner.tradingview.com` | yes | `mk_stock_quotes`, `mk_stock_screen` |
| news | the RSS/Atom list in `news.FEEDS` or config `feeds` | yes | `mk_news`, `mk_news_polls`, `mk_routes`, `mk_story_places` |
| sports | `site.api.espn.com` scoreboard | yes | `mk_sports_games` |
| books | the feed db | n/a | the ledger: marks, funding, rails, settlement |

No key is needed anywhere. If you add a keyed source, read the key from an env file path in the config and never print it; nothing here does.

## Paper books

Ledger tables: `bk_books` (one per seat and lane: `pm-<role>`, `perps-<role>`, `stocks-<role>`, start cash 1000), `bk_positions`, `bk_events`. A fill is a model priced from the newest fetched tick; `proof_json` keeps price, quote time, source and model id. Stale quote (120 s; stocks 180 s), unknown market, closed session, insufficient depth, cash, max open (5), margin cap (25 percent of start cash per position) and a halted book refuse with a reason. An open position without a fresh price stays open and unpriced; equity is then unknown (cash shown). Orders need `thesis`, `disproof`, `exit_plan`, tp and sl.

- polymarket: shares = usd / (ask + fee), at the best ask; exit at the best bid less the fee; resolution pays 1 or 0 per share with no fee, read from Gamma `closed` with prices exactly 0 or 1. tp/sl are token prices on the bid.
- perps: fill = mid +- slippage (2 bps), fee 4.5 bps of notional each side, leverage cut to `min(asked, config cap 5, coin max, 50 / sl_pct)`, liquidation price `entry - s*entry*(1/lev - l)/(1 - s*l)` with `l = 1/(2*max leverage)`, moved by funding paid; funding each engine tick = `rate/interval_h * dt_h * qty * mark`, longs pay when positive; liquidation loses the whole margin; max hold 4 h.
- stocks: fill = last +- 2 bps, regular session only, none in the last 30 min, flat at close - 5 min, leverage <= 5, liquidation at 90 percent of margin lost.
- Not modelled (results are optimistic): queue and landing delay, depth beyond the best level (a larger order is refused), intrabar rail walks (a rail fires on the tick that sees it and exits at that tick's price), funding between ticks, stock borrow and halts, partial liquidation, the clock offset file (host clock is used).

## Seat tools

| group | tools |
|---|---|
| polymarket | `pm_markets`, `pm_market`, `pm_order`, `pm_exit`, `pm_book`, `pm_strategies`, `pm_strategy_register`, `news_call`, `call_scores`, `sports_games` |
| perps | `perp_markets`, `perp_market`, `perp_candles`, `perp_order`, `perp_exit`, `perp_book`, `perp_strategies`, `perp_strategy_register` |
| stocks | `stock_quotes`, `stock_quote`, `stock_screen`, `stock_order`, `stock_exit`, `stock_book`, `stock_strategies`, `stock_strategy_register` |
| news | `story_list(room)`, `story_read`, `story_route`, `story_place`, `meme_list`, `story_debate`, `story_debates`, `explorer_note`, `explorer_notes`, `lesson_add`, `lessons_read`, `skill_save`, `skills_read`, `review_add`, `video_rule_add`, `video_rules` |

Order tools are removed per seat with `denied_tools` (`seatgen` does it). Wake sources (seat config): `story_rooms` + `story_wake_min_score` (60) + `story_wake_min_s` (300); `book_lanes` (closed position once, alert once per trigger: `alert_loss_pct` 15, `alert_gain_pct` 50, `alert_liq_pct` 20); `candle_wakes_pct` + `perp_candle_coins`. Once-only marks: `mk_wakes` in the seat's office db.

## Router

`router.classify(item, market_rows, hook)` -> rooms `stocks, crypto, memes, polymarket, perps (macro and majors), sports, other`, each with a 0 to 100 rule score and a reason; routes under 30 are dropped, a story with none goes to `other`. Inputs: keyword sets, a source prior, overlap of rare words with open market questions (sum of idf, two shared words or one rare), meme markers (a strong one, or two weak ones) with a proposed name and ticker, places. A cross-source duplicate (same title within 6 h) is stored with `dup_of` and routed once. `story_route` by a seat adds a route with classifier `seat:<role>`.

## API for the floor rooms (`sniper_office.markets.api`)

Each takes open connections `feed` (markets_db) and `led` (markets_ledger_db) (read-only is enough; `C.ro(path)`), `now`, and `pol = books.policy(cfg)` where a position is priced.

| room | call | shape |
|---|---|---|
| Newsroom | `newsroom(feed, led, now, pol)` | `wire[]` {news_id, source, title, url, published, fetched_at, age_s, routes[{room,score,why,classifier,meme,ticker}], also_from[]}, `calls[]` positions naming a story with live PnL, `news_calls[]`, `debates{news_id:[{role,stance,text,at}]}`, `memes[]`, `feeds[]`, `room_counts{}` |
| Memes | `meme_candidates(feed, now)` | [{news_id,title,source,score,name,ticker,why,age_s,proposed_by}] |
| Situation | `situation(feed, now)` | `stories[]` {news_id,title,source,url,age_s,rooms,places[{code,name,lat,lon,classifier}]}, `places[]` with counts |
| Strategy Lab | `strategy_lab(led)` | `strategies[]` {strategy_id,name,book,thesis,entry,exit,fails_when,status,settled_n,wins,settled_mean_pct,settled_sum_pct,open_n,verdict}, `totals_by_lane{}` |
| Paper Labs | `paper_labs(feed, led, now, pol)` | `news_calls[]` {call_id,side,entry_ask,horizon,marks{15m,1h,6h,24h:{state,call_pp,control_pp}},resolved_pp}, `books[]`, `strategies{}`, `not_ported` |
| Sports | `sports(feed, now)` | `games[]` (with-vig first-book implied odds and de-vigged fair home/away/draw), `edges[]` (independent token asks, fee, spread, quote age, model gap and paired-quote gap), `edges_status`, `feeds[]` |
| Explorer | `explorer(feed, led, now)` | `notes[]`, `leads[]` {question, side, price_at, bid_now, resolved_pp} |
| Trading | `trading(feed, led, now, pol)` | one `book_view` per book: cash, equity (null when unpriced), realized_total, unrealized_total, realized_today, closed_n, wins, `open[]` with `live`, `closed_recent[]` |
| Coach and skills | `coach(led)` | `lessons[]`, `skills[]` (every version), `reviews[]` |
| Video rule lab | `video_lab(led)` | `rules[]` |
| Control | `control(feed, led, now, pol)` | `feeds[]` {feed,age_s,ok,detail}, `books[]` {halted,cash}, `universe{}`, `events[]`, `operator_chat[]`, `jobs[]` |

The existing ESPN provider scope is EPL, NFL, NBA, MLB and NHL. The parser also handles dated soccer scoreboards such as La Liga, Champions League and Europa League when collection from those leagues is authorized. The collector spaces ESPN requests by at least 10 seconds and timestamps each response separately. Raw implied probabilities come from the first complete provider. Fair probabilities average each distinct provider's complete de-vigged line, including the draw for soccer. Provider update timestamps are nullable and distinct from the local poll time. Polymarket's sports-tag fixtures have a separate collection budget so they do not replace temperature or post-count buckets.

The hold-to-resolution gap is `fair - ask - entry fee`. The executable ask already incorporates the entry spread. The distinct trader hurdle compares `fair - ask` to `3 × (entry fee + full spread + exit fee at the bid)`. The sports feed's `min_bookmakers` defaults to two. A one-book comparison is always labeled low trust and can qualify only when the operator sets the minimum to one and no other low-trust reason applies. A newly observed line is low trust for its first five minutes. A line observed unchanged for more than five minutes while the PM midpoint moves more than three points is low trust. The timestamp describes our observations, with a reset after a coverage gap. A paired PM quote is a separate price observation and has no fill guarantee. Sports does not place orders.


Also registered as operator-page panels through `host.add_panel` (`markets_books`, `markets_feeds`, `markets_stories`) when the host config names the two dbs: no `ui.py` change is needed for those.

Write actions for `ui.py`'s token-gated POST path (both need `X-Office-Token`, a fixed key set, and the ledger opened read-write): `POST /api/markets/halt` body `{book_id, halted, reason}` -> `api.book_halt(led, book_id, halted, 'operator', now, reason)`; `POST /api/markets/say` body `{to_role, text}` -> `api.operator_say(led, office_db, to_role, text, now)`. Use a role, `all`, or an empty role to address the active roster. Control shows per-note delivery totals. Each recipient host includes up to eight pending notes per regular wake and records a receipt after a successful wake. Failed wakes leave notes pending. Rows recorded before receipt tracking are labeled as legacy.

SQL for `floor/sources.json` can read the same tables directly: `mk_news`, `mk_routes`, `mk_story_places`, `mk_pm_markets`, `mk_pm_quotes`, `mk_perp_ticks`, `mk_perp_candles`, `mk_stock_quotes`, `mk_sports_games`, `bk_books`, `bk_positions`, `bk_events`, `bk_calls`, `bk_strategies`, `bk_debates`, `bk_explorer`, `bk_leads`, `bk_lessons`, `bk_skills`, `bk_reviews`, `bk_video_rules`, `bk_operator_chat`. `config/markets_data_sources.example.json` lists the ones `db_read` may show.

## Install

1. Copy `config/markets_<name>.example.json` to `run/config/markets_<name>.json` for polymarket, perps, stocks, news, sports, intel_hl, books; set `markets_db`, `ledger_db`.
2. Copy the units to `~/.config/systemd/user/`, set the paths, `systemctl --user enable --now office-markets-feed@{polymarket,perps,stocks,news,sports,books} office-markets-intel-hl`.
3. Seats: `python3 -m sniper_office.markets.seatgen --head run/config/seat_head.json --out-dir run/config` writes `seat_<role>.json` for newsdesk, pm_trader, pm_critic, perps_trader, stocks_trader, meme_scout, explorer, coach (codex keys copied from the head, actor `ag_`+16 hex, kept on rerun) and prints the roster rows to insert into `office_v2_roster`; copy `sniper_office/markets/prompts/<role>.md` to `run/config/prompts/`; merge `config/markets_data_sources.example.json` into the `data_sources` file; create each seat's workspace and state directories as for the others.

## The floor's newer rooms

`floor/routes.py` (loaded by `ui.py` from the floor folder, see `docs/UI.md` 10.1) and `floor/static/office-live.js` fill every room from a real source and say unknown, or empty, when the source is missing. All text from feeds and seats goes in with `textContent`.

| room | panels | read from |
|---|---|---|
| Newsroom | stories (routes, duplicates folded), debates, news calls, calls on stories, feeds | `GET /api/office/markets` -> `api.newsroom` |
| Situation | the world map: stories with places, pulsing pins | `api.situation` (the existing map component) |
| Sports | games, de-vigged lines, edges, feed | `api.sports` |
| Strategy Lab | strategies with verdicts, totals by lane | `api.strategy_lab` |
| Paper Labs | news-call lab, every book, lane totals | `api.paper_labs` |
| Explorer | briefs, leads, wallet findings, creator launches | `api.explorer`; `sources.json` `wallet_findings` (`--db wallet=`, default `wallet_network.db`); `/api/office/services` (creator checkpoint, `--creator-dir`) |
| Memes | meme candidates, narrative guesses, launch matches, X mentions | `api.meme_candidates`; `sources.json` `narrative_*` (`narrative.db`), `x_mentions` (`x.db`) |
| Coach and skills, Video rule lab | lessons, every skill version, reviews; rules under test | `api.coach`, `api.video_lab` |
| Control | host guard file (`--guard-file`), systemd unit states, J7 listener liveness (newest stored signal: the listener keeps no status table), tick recorder, ticks resolution, crew bridge cursor, creator collector, markets feed health, timed jobs, book halt buttons, operator note | `GET /api/office/services`, `api.control`; writes `POST /api/markets/halt` and `/api/markets/say` (token; the page asks for it) |
| Trading | all books with realized and unrealized PnL (the sum is in the header), copy desk positions with live charts and tp/sl lines, J7 signals with the copy desk's decision, copy desk orders and fills, Polymarket, perps and stocks books, the memecoin judges' ledgers | `GET /api/office/live` (about once a second), `api.paper_labs`, `sources.json` `orders`, `requests`, `outcomes` |

`GET /api/office/live[?since=T]`: the J7 book rows and open positions (`desk_live_pnl`), tp/sl from each order's frozen policy, the signals of the last hour, and the 1 s ticks of every coin shown newer than `since`; the page rewrites every live value in place. The rails act on the net exit value, so the tp and sl lines on a chart are drawn from the entry price and are approximate. The provider's price changes about every 30 s although we poll each second; the page prints the measured gaps. A book with an open position it could not price has no unrealized figure, and the header total says so instead of counting zero.

Animations (skipped under `prefers-reduced-motion`): a J7 buy pings the copy desk (pulse ring, bubble with trader, chain, coin, size) and goes to the top of the live feed; a routed story flies as a card from the Newsroom to its room; mail flies as an envelope from the sender's desk to the receiver's (crew mail starts at Control).

Coins are shown as `Name ($SYMBOL)`, with the short address and a copy button as a small second detail. `GET /api/office/token-names?ids=chain:mint,...` answers from J7 signal symbols and the narrative launch table first, then from a host-side cache table (`--names-db`, default `token_names.db` beside the office store) filled by one DexScreener `tokens/v1` lookup at a time on a background thread (https only, one host, no redirects, at most one request per 1.2 s, a miss retried after 15 minutes). Provider text is cleaned and capped on the host and again in the page; seats never read this table.
