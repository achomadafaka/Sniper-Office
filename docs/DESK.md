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
| `ledger_db` | book_view, paper_order, paper_exit, results_view | `data/office/v2/explore-ledger/ledger.db` (GPT judge: `.../explore-gpt-ledger/ledger.db`) |
| `results_ledgers` | results_view (list; default `[ledger_db]`) | both ledgers above |
| `ledger_epoch` | book id in the ledger (default: the seat epoch) | `office-v2-explore-gpt-1` for the GPT judge |
| `book_policy` | book_view, paper_order rail bounds | `config/office/book_policy_pilot.json` |
| `clock_file` | feed_list, book_view, candidate_register | `data/clock_offset.json` |
| `research_env_file` | chain_*, network_expand (needs `MEMECOIN_RESEARCH_HELIUS_RPC_URL`, https) | the research env file |
| `chain_db` | chain cache and pace (default: beside `office_db`) | |
| `wallet_network_db` | network_read, network_expand | `data/office/wallet_network.db` |
| `brain_env`, `brain_script`, `brain_python` | brain_read, brain_note | env file, `scripts/office_brain.py`, a python that can import the brain |
| `feed_window_s`, `feed_stale_s` | feed_list (defaults 3600, 900) | |

## What each tool is here

- `results_view`, `book_view`, `candidate_register`, `feed_filter_set`, `book_halt`: real, against the ledger files (read-only) and the seat's own office db (tables `desk_*`). `book_view` equity is exact for a flat book; with a position open it is `unknown` (valuation is not built here), cash is shown.
- `feed_list`, `sheet_read`: the stored hard-filter passes and their stored evidence, state `partial`. The near-pass feed and the live sheet builder stay in the source project. A desk db whose newest ticket is older than `feed_stale_s` is `unknown`, not an empty feed.
- `paper_order`, `paper_exit`: checked (role, plan, rails, halt, one entry per coin, order open in the book), then recorded in `desk_paper_requests` with state `queued`, `filled: false`. Nothing in this package fills them.
- `chain_*`, `network_*`: the source project's read-only chain data and wallet network, ported as `chain_data.py` and `wallet_network.py`.
- `brain_*`: run the configured brain script as a subprocess, one call per process.
