"""Write the markets seats' config files from an existing seat config (a head seat's): `python3 -m sniper_office.markets.seatgen --head run/config/seat_head.json --out-dir DIR [--root run] [--prompts-dir DIR]`.
Copies the account, model and runtime keys (codex family, model, binaries and hashes, sdk_version, office_db, quota_db, denylist, search policy, data_sources) and sets the per-seat keys (actor, role, state paths, prompt_file, plugins,
tool_groups, denied_tools, markets_* paths, wake keys). An actor id already present in DIR/seat_<role>.json is kept, so a rerun does not change identities. Prints the roster rows to insert. Writes nothing outside --out-dir."""
import argparse
import json
import secrets
import sys
from pathlib import Path

ORDERS = {'polymarket': ['pm_order', 'pm_exit'], 'perps': ['perp_order', 'perp_exit'], 'stocks': ['stock_order', 'stock_exit']}
SEATS = {   # role: (tool_groups, order lanes it may use, story_rooms, book_lanes, extras)
    'newsdesk': (['news', 'polymarket'], [], ['polymarket', 'perps', 'stocks', 'crypto', 'sports', 'other'], [], dict(story_wake_min_score=70)),
    'pm_trader': (['polymarket', 'news'], ['polymarket'], ['polymarket'], ['polymarket'], {}),
    'pm_critic': (['polymarket', 'news'], [], [], [], {}),
    'perps_trader': (['perps', 'news'], ['perps'], ['perps'], ['perps'], dict(candle_wakes_pct=2.0, perp_candle_coins=['BTC'])),
    'stocks_trader': (['stocks', 'news'], ['stocks'], ['stocks'], ['stocks'], {}),
    'meme_scout': (['news'], [], ['memes'], [], dict(story_wake_min_score=50)),
    'explorer': (['polymarket', 'news'], [], [], [], {}),
    'coach': (['polymarket', 'perps', 'stocks', 'news'], [], [], [], {}),
}
DROP = ('desk_db', 'ledger_db', 'book_policy', 'results_ledgers', 'research_env_file', 'wallet_network_db', 'chain_db', 'ticket_wakes', 'seat_added', 'account_poller')


def build(head, role, root, markets_db, ledger_db, prompts_dir, actor, state_root):
    groups, lanes, rooms, books, extra = SEATS[role]
    cfg = {k: v for k, v in head.items() if k not in DROP}
    denied = sorted(t for lane, names in ORDERS.items() if lane not in lanes for t in names)
    cfg.update(actor=actor, role=role, host_state=f'{root}/data/seat-{role}.json', ca_dir=f'{root}/data/ca/{role}', proxy_log_dir=f'{root}/data/proxy/{role}', workspace=f'{state_root}/{role}/workspace',
               state_dir=f'{state_root}/{role}/state', prompt_file=f'{prompts_dir}/{role}.md', plugins=['sniper_office.desk', 'sniper_office.markets'], tool_groups=['brain'] + groups, denied_tools=denied,
               markets_db=markets_db, markets_ledger_db=ledger_db, account_poller=False, **extra)
    if rooms:
        cfg['story_rooms'] = rooms
    if books:
        cfg['book_lanes'] = books
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--head', required=True, help='an existing seat config to copy the account and runtime keys from')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--root', default=None, help='the Office run directory (default: the parent of the head config\'s office_db directory)')
    ap.add_argument('--prompts-dir', default=None, help='where the seat prompts will live (default: <root>/config/prompts)')
    ap.add_argument('--state-root', default='/var/lib/sniper-office')
    ap.add_argument('--roles', default=','.join(SEATS))
    args = ap.parse_args(argv)
    head = json.loads(Path(args.head).read_text())
    root = args.root or str(Path(head['office_db']).parent.parent)
    prompts = args.prompts_dir or f'{root}/config/prompts'
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for role in [r for r in args.roles.split(',') if r in SEATS]:
        path = out / f'seat_{role}.json'
        actor = json.loads(path.read_text())['actor'] if path.exists() else 'ag_' + secrets.token_hex(8)
        cfg = build(head, role, root, f'{root}/data/markets.db', f'{root}/data/markets_ledger.db', prompts, actor, args.state_root)
        path.write_text(json.dumps(cfg, indent=1) + '\n')
        rows.append((actor, head['epoch'], role, head.get('generation', 1)))
    print('wrote %d seat configs to %s' % (len(rows), out))
    for actor, epoch, role, gen in rows:
        print("roster: INSERT OR IGNORE INTO office_v2_roster VALUES('%s','%s','%s',%d,1,0,strftime('%%s','now'));" % (actor, epoch, role, gen))
    return 0


if __name__ == '__main__':
    sys.exit(main())
