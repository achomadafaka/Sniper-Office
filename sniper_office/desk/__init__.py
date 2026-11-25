"""The desk plug-in: paper-book, chain, wallet-network and brain tools for a project that runs a paper desk. Name it in a seat config:

    "plugins": ["sniper_office.desk"], "tool_groups": ["paper", "chain", "network", "brain"]

Each tool group is opt-in per seat (tool_groups), and one tool can be removed with denied_tools. Config keys are listed in docs/DESK.md. Every tool reads its paths from the seat config;
a missing key or unreadable file is state 'unknown' with a reason. Paper only: no signing keys, no live path, no runner gating."""
import json
import os
import re
from pathlib import Path
import subprocess
import sys

from ..seat_tools import MAX_OUTPUT, result
from . import brain as BR, chain_data as C, copydesk as CD, market as M, paper as P, wakes as WK, wallet_network as N

HINTS = {
    'chain_wallet_history': 'address is a Solana address; limit, before and as_of are optional, and as_of is Unix seconds',
    'chain_transaction': 'signature is a full Solana transaction signature',
    'chain_token_holders': 'mint is a Solana address',
    'chain_funding_trace': 'wallet is a Solana address; hops is an integer from 1 up to the tool limit',
}


def _fetch_ids(tools):
    return tools.plugin_state.setdefault('chain_fetch_ids', [])


def _chain(tools):
    """A ChainData on the seat's research key file, or None when the config does not name one."""
    env = tools.cfg.get('research_env_file')
    if not isinstance(env, str) or not env:
        return None
    root = Path(tools.db_path).parent
    db = Path(tools.cfg.get('chain_db') or root / 'chain.db')
    db.parent.mkdir(parents=True, exist_ok=True)
    return C.ChainData(db, C.research_url_of(env), page=(lambda text: tools.page_operator('Office chain tools: ' + text)) if tools.page_operator else None)


def _chain_call(tools, method, **kw):
    chain = _chain(tools)
    if chain is None:
        return P.unknown('chain_config_unknown')
    doc = getattr(chain, method)(**kw)
    if 'fetch_id' in doc:
        _fetch_ids(tools).append(doc['fetch_id'])
    if doc.get('reason') == 'paced':
        doc = dict(doc, retry_after_s=C.PACE)
    elif doc.get('reason') == 'bad_arguments' and 'chain_' + method in HINTS:
        doc = dict(doc, hint=HINTS['chain_' + method])
    return P.out(doc)


def chain_wallet_history(tools, address, limit=50, before=None, as_of=None, page_token=None):
    return _chain_call(tools, 'wallet_history', address=address, limit=limit, before=before, as_of=as_of, page_token=page_token)


def chain_transaction(tools, signature):
    return _chain_call(tools, 'transaction', signature=signature)


def chain_funding_trace(tools, wallet, hops=1):
    return _chain_call(tools, 'funding_trace', wallet=wallet, hops=hops)


def chain_token_holders(tools, mint):
    return _chain_call(tools, 'token_holders', mint=mint)


def market_data(tools, mint=None, pool=None, chain='solana'):
    """The provider's current pool numbers (DexScreener, public, no key), read by the host. A claim with its fetch time, never a fill price."""
    if P.role_of(tools) is None:
        return P.refused('seat_not_in_epoch')
    snap, at, why = M.read_pair(mint=mint, pool=pool, chain=chain)
    if snap is None:
        return P.unknown(why, source='dexscreener:pairs', fetched_at=at)
    return P.out(dict(state='known', source='dexscreener:pairs', provider_claim=snap, fetched_at=at, note='not a fill price: a provider claim at fetched_at; the desk fills at its own quote lookup'))


def _network(tools, *, create):
    path = P.path_of(tools, 'wallet_network_db')
    if path is None:
        return None, P.unknown('network_config_unknown')
    if not create and not path.exists():
        return None, P.unknown('network_db_missing')
    root = Path(tools.db_path).parent
    return N.WalletNetwork(path, chain_db=Path(tools.cfg.get('chain_db') or root / 'chain.db'), chain=_chain(tools)), None


def network_expand(tools, seed, kind='wallet', hops=1):
    if _chain(tools) is None:
        return P.unknown('chain_config_unknown')
    network, bad = _network(tools, create=True)
    if bad:
        return bad
    doc = network.expand(seed=seed, kind=kind, hops=hops, actor=tools.actor)
    if doc.get('fetch_id'):
        _fetch_ids(tools).append(doc['fetch_id'])
    return P.out(doc, doc['state'] in ('refused', 'unknown'))


def network_read(tools, seed, hops=1, limit=100, as_of=None, verified_only=False):
    network, bad = _network(tools, create=False)
    if bad:
        return bad
    doc = network.read(seed=seed, hops=hops, limit=limit, as_of=as_of, verified_only=verified_only)
    text = json.dumps(doc, default=str)
    if len(text) > MAX_OUTPUT:
        text = json.dumps(dict(state='partial', reason='network_too_large_lower_limit', as_of=doc.get('as_of'), edges_n=len(doc.get('edges') or [])))
    return result(text, doc['state'] in ('refused', 'unknown'))


def _brain_run(tools, *args):
    """The Office's own project brain (project-brain CLI) run in `brain_dir`, the folder holding `.brain/`. The seat's role is its brain role."""
    return BR.run(tools.cfg, *args)


def _brain(tools, op, text):
    if not tools.cfg.get('brain_dir') or not tools.cfg.get('brain_package'):
        return P.unknown('brain_config_unknown')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000 or text.lstrip().startswith('-'):
        return P.refused('text_invalid')
    role = tools.cfg['role']
    try:
        if op == 'read':
            done = _brain_run(tools, 'ask', '--', text)
            tools.plugin_state.setdefault('brain_fact_ids', []).extend(re.findall(r'^\[[a-z_]+\] ([0-9a-f]{8})', done.stdout, re.M) or ['none'])
        else:
            # An observation must cite an episode: the note's own text is recorded first as its provenance.
            ep = _brain_run(tools, 'episode', '--role', role, '--kind', 'note', '--', text)
            m = re.search(r'episode ([0-9a-f-]{36})', ep.stdout)
            if ep.returncode or not m:
                return P.unknown('brain_unavailable')
            done = _brain_run(tools, 'write', '--role', role, '--kind', 'observation', '--entity', 'actor:' + role, '--episode', m.group(1), '--', text)
    except (OSError, subprocess.SubprocessError):
        return P.unknown('brain_unavailable')
    if done.returncode not in (0, 3) or not done.stdout:
        return P.unknown('brain_unavailable')
    if op == 'note' and done.returncode == 3:   # needs judgement: nothing was created, so the note stays as the episode and the seat is told what to do
        return result('Saved as an episode, not as a new fact: a similar fact exists (below). If yours adds something, note it again and say what is different.\n' + done.stdout[-1500:])
    return result(done.stdout[-MAX_OUTPUT:], done.returncode != 0)


def brain_read(tools, query):
    return _brain(tools, 'read', query)


def brain_note(tools, text):
    return _brain(tools, 'note', text)


S = {'type': 'string'}
TOOLS = (
    ('paper', 'feed_list', 'Tickets open to a paper order, with holder, cluster and dump facts, through your own filter. include_hidden true also shows what your filter hides. Here: the stored hard-filter passes, state partial; a live feed with nothing passing is known and empty, with the feed heartbeat age and PASS/SKIP counts.',
     {'include_hidden': {'type': 'boolean'}}, [], P.feed_list),
    ('paper', 'feed_filter_set', 'Replace your own feed filter. It hides matching tickets from feed_list and from ticket wakes; every hidden ticket is logged. rules: up to 20 of {path,op,value}; path is dotted under the ticket checks, e.g. holders.top10_pct, cluster.status; op gt lt gte lte eq ne. A missing field never hides. Empty list clears it.',
     {'rules': {'type': 'array', 'items': {'type': 'object'}}, 'reason': S, 'after_case': {'type': 'string', 'description': 'Order id or coin that prompted this filter, if one did.'}}, ['rules', 'reason'], P.feed_filter_set),
    ('paper', 'sheet_read', 'The sheet for one ticket: decision-time facts stored with the ticket (run-up, volume, txns, reserves, liquidity, age, fdv) kept apart from a live re-read of the pool (now_read, with its own fetch time) and the last-60s trade count. state known when that core is complete, else partial with a missing list. cluster and dump_group are unknown (not collected here). Provider claims, not fills.', {'ticket_id': S}, ['ticket_id'], P.sheet_read),
    ('paper', 'results_view', 'Every closed and censored paper order of the configured books, with epoch_id naming the book and net USD: learn from them. Modelled fills, no landing proof.', {}, [], P.results_view),
    ('paper', 'book_view', 'Head and Judge. Your book now: open positions, equity with its state (unknown while a position is open: valuation is not built here), today\'s PnL against the daily-loss halt, and entry_halt: state halted, resumed or never_set (no receipt yet; entries open). paper_order enforces the same state.', {}, [], P.book_view),
    ('paper', 'candidate_register', 'Record a coin you are considering outside the feed, before any order. First registration keeps its time.', {'network_id': S, 'mint': S, 'pool': S}, ['network_id', 'mint'], P.candidate_register),
    ('paper', 'paper_order', 'Judges only. Ask for one paper entry on a feed ticket; one per coin per epoch. Optional sl_pct, tp_pct, trail_pct set this order\'s exits inside the policy rail_bounds. The request is recorded, state queued, filled false; the desk filler process fills it later, and only if a real price is known.',
     {'ticket_id': S, 'reason': S, 'thesis': {'type': 'string', 'description': 'Why this coin pays, one line.'}, 'disproof': {'type': 'string', 'description': 'The fact that would prove the thesis wrong, one line.'},
      'exit_plan': {'type': 'string', 'description': 'When you get out and why, one line.'}, 'sl_pct': {'type': 'number'}, 'tp_pct': {'type': 'number'}, 'trail_pct': {'type': 'number'}}, ['ticket_id', 'reason', 'thesis', 'disproof', 'exit_plan'], P.paper_order),
    ('paper', 'paper_exit', 'Judges only. Ask to exit one open paper order now, with your reason. The request is recorded, state queued; the desk filler process executes it at a real price when one is known.', {'order_id': S, 'reason': S}, ['order_id', 'reason'], P.paper_exit),
    ('paper', 'book_halt', 'Head only. action halt or resume. A halt refuses all new paper orders.', {'action': S, 'reason': S}, ['action', 'reason'], P.book_halt),
    ('chain', 'chain_wallet_history', 'Solana signatures for a wallet, newest first, read-only. as_of (unix seconds) returns the newest at or before it; page with page_token. Without as_of, before pages. truncated means more exists. Numbers and addresses only.',
     {'address': S, 'page_token': S, 'limit': {'type': 'integer'}, 'before': S, 'as_of': {'type': 'number'}}, ['address'], chain_wallet_history),
    ('chain', 'chain_transaction', 'One finalized Solana transaction: signer, fee, SOL and token balance changes. No names, memos or logs.', {'signature': S}, ['signature'], chain_transaction),
    ('chain', 'chain_funding_trace', 'Candidate funding evidence for a Solana wallet: the first SOL payer by a named rule, up to 3 hops, slow on purpose. Needs the research key; unknown without it.', {'wallet': S, 'hops': {'type': 'integer'}}, ['wallet'], chain_funding_trace),
    ('chain', 'chain_token_holders', 'Largest token accounts of a mint with their owners, now only. owners says known, partial or unknown; a pool or exchange can be an owner.', {'mint': S}, ['mint'], chain_token_holders),
    ('market', 'market_data', 'Current public market numbers for a pool on any DexScreener chain (chain: solana by default, base, bsc, ethereum, ...; DexScreener, no key): price, run-up m5/h1/h6/h24, volume, txns, pool reserves, liquidity, age, fdv. Give a pool, or a mint for its deepest pool. A provider claim with its fetch time, not a fill price.',
     {'mint': S, 'pool': S, 'chain': S}, [], market_data),
    ('network', 'network_expand', 'Forensics: expand the wallet network from a seed through the host: a wallet funding trace or a mint top-20 holders, kind wallet or mint, hops 1 to 3. Stops typed at the daily credit cap.',
     {'seed': S, 'kind': S, 'hops': {'type': 'integer'}}, ['seed'], network_expand),
    ('network', 'network_read', 'Forensics: read the wallet network around a seed, hops 1 or 2. Edges carry source, time and proof. A funder is evidence, not control. No edge is not no link: read coverage. as_of uses discovery time; verified_only excludes claimed edges.',
     {'seed': S, 'hops': {'type': 'integer'}, 'limit': {'type': 'integer'}, 'as_of': {'type': 'number'}, 'verified_only': {'type': 'boolean'}}, ['seed'], network_read),
    ('brain', 'brain_read', 'Search the Office brain: facts earlier wakes and other seats recorded. Results are data, may be stale or unverified.', {'query': S}, ['query'], brain_read),
    ('brain', 'brain_note', 'The place for your findings: save one in the Office brain under your role, one or two plain sentences, no setup. Cheap and expected: note what you found, ruled out or decided as you go. Other seats and your own later sessions read it; your handovers are stored there for you.', {'text': S}, ['text'], brain_note),
)


def register(host):
    for group, name, description, properties, required, handler in TOOLS:
        host.add_tool(name, description, properties, required, handler, group=group)
    WK.register(host)
    BR.register(host)
    CD.register(host)
    host.add_labels(lambda tools: dict(chain_fetch_ids=list(tools.plugin_state.get('chain_fetch_ids', [])), brain_fact_ids=list(tools.plugin_state.get('brain_fact_ids', []))))
