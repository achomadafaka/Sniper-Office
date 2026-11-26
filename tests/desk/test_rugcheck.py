"""Rug detectors ported into the hard filter: PumpSwap LP custody, wallet-only holder concentration, the funder cluster. Fake rpc callables only."""
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_feed import CREATOR, HOLDER, LP_ACCT, LP_MINT, MINT, POOL, addr, rig, rows  # noqa: E402,F401  (rig is a fixture)

from sniper_office.desk import feed as F  # noqa: E402
from sniper_office.desk import rugcheck as RC  # noqa: E402
from sniper_office.desk.onchain import b58  # noqa: E402


def pump_address(n):
    """A real 32-byte address whose text ends in 'pump' (a launchpad mint)."""
    k = 58 ** 4
    value = int.from_bytes(bytes([n]) * 32, 'big') // k * k + sum(RC.B58_ALPHABET.index(c) * 58 ** i for i, c in enumerate(reversed('pump')))
    return b58(value.to_bytes(32, 'big'))


class Net:
    """rpc(method, params) -> (result, why) from per-method handlers; counts calls."""
    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, []

    def rpc(self, method, params):
        self.calls.append(method)
        out = self.handlers[method](params) if method in self.handlers else None
        return (out, None) if out is not None else (None, 'rpc_error')


def pool_acct(mint, creator=CREATOR, owner=RC.PUMP_AMM, quote=F.WSOL):
    raw = bytes(11) + RC._decode(creator) + RC._decode(mint) + RC._decode(quote) + RC._decode(LP_MINT) + bytes(64) + bytes(8)
    return dict(owner=owner, data=[base64.b64encode(raw).decode(), 'base64'])


def lp_net(mint, supply, lp_owner=None, **kw):
    owned = lambda o: dict(value=dict(owner='Tokenkeg', data=dict(parsed=dict(info=dict(owner=o)))))
    return Net(getAccountInfo=lambda p: dict(value=pool_acct(mint, **kw)) if p[0] == POOL else owned(lp_owner),
               getTokenSupply=lambda p: dict(context=dict(slot=5), value=dict(amount=str(supply))),
               getTokenLargestAccounts=lambda p: dict(value=[dict(address=LP_ACCT, amount=str(supply))]))


PAIR = dict(dexId='pumpswap')
LP = lambda net, mint=MINT: RC.lp_check(net, lambda: 1., PAIR, mint, POOL)


def test_on_curve_matches_the_reference_library_vectors():
    wallets = ['5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9', 'EhQSwvZvNZjzjPPVB3z9WZkPkvqg398nVJoHZV24MyFB', 'Ctfm9V1NpALJXrz1j9qRyQwgRMKP2J4suX5V5NfpvWp3']
    pdas = ['8aedJXfFLMytBaukyaLqCyhXCaVe3HGrDYZvsYgadg4V', 'Aa9NQ88wh8b4acwxhodJvfre2hXEcYxzaBq9YmpQv668', '9mpCrLuwecdTthrRgnU2t12eu85RXqgNptcug3uA6Bok']
    assert [RC.on_curve(a) for a in wallets] == [True] * 3 and [RC.on_curve(a) for a in pdas] == [False] * 3
    assert RC.on_curve('short') is None and RC.on_curve('0' * 40) is None


def test_lp_not_read_for_other_dexes():
    row = RC.lp_check(Net(), lambda: 1., dict(dexId='raydium'), MINT, POOL)
    assert (row['status'], row['reason']) == ('unknown', 'lp_reader_not_available')


def test_lp_burned_passes_at_supply_zero():
    row = LP(lp_net(MINT, 0))
    assert (row['status'], row['reason'], row['slot']) == ('pass', 'lp_burned', 5)


def test_lp_held_by_the_creator_kills_even_on_a_launchpad_mint():
    mint = pump_address(7)
    row = LP(lp_net(mint, 100, lp_owner=CREATOR), mint)
    assert (row['status'], row['reason'], row['largest_holder_owner']) == ('kill', 'lp_held_by_creator', CREATOR)


def test_lp_outstanding_on_a_mint_that_is_not_a_launchpad_migration_kills():
    row = LP(lp_net(MINT, 100, lp_owner=addr(77)))
    assert (row['status'], row['reason'], row['launchpad_migrated']) == ('kill', 'lp_held_by_creator', False)


def test_lp_outstanding_on_a_launchpad_mint_held_by_someone_else_is_unknown_not_pass():
    mint = pump_address(7)
    row = LP(lp_net(mint, 100, lp_owner=addr(77)), mint)
    assert (row['status'], row['reason']) == ('unknown', 'lp_outstanding_holder_unproven')


@pytest.mark.parametrize('net,reason', [
    (Net(), 'rpc_error'),                                                                                  # pool unreadable
    (lp_net(MINT, 0, owner='SomeProgram'), 'lp_pool_not_pumpswap'),
    (lp_net(MINT, 0, quote=addr(3)), 'lp_pool_mismatch'),
    (Net(getAccountInfo=lambda p: dict(value=pool_acct(MINT))), 'rpc_error'),                              # supply unreadable
    (Net(getAccountInfo=lambda p: dict(value=pool_acct(MINT)), getTokenSupply=lambda p: dict(context=dict(slot=0), value=dict(amount='0'))), 'lp_supply_unusable')])
def test_lp_unreadable_is_unknown(net, reason):
    row = LP(net)
    assert (row['status'], row['reason']) == ('unknown', reason)


def test_lp_burned_needs_two_calls_and_an_outstanding_lp_five():
    burned, held = lp_net(MINT, 0), lp_net(MINT, 5, lp_owner=CREATOR)
    LP(burned), LP(held)
    assert len(burned.calls) == 2 and len(held.calls) == 4


def test_owner_exclusions_remove_program_accounts_and_the_incinerator_and_refuse_to_guess():
    wallet, pda = 'EhQSwvZvNZjzjPPVB3z9WZkPkvqg398nVJoHZV24MyFB', '8aedJXfFLMytBaukyaLqCyhXCaVe3HGrDYZvsYgadg4V'
    net = Net(getMultipleAccounts=lambda p: dict(value=[dict(owner=RC.SYSTEM_PROGRAM) if o == wallet else dict(owner='SomeProgram') for o in p[0]]))
    out, why = RC.owner_exclusions(net, [wallet, pda, RC.INCINERATOR])
    assert out == {wallet: None, pda: 'program_account:SomeProgram', RC.INCINERATOR: 'incinerator'} and why is None
    nothing = Net(getMultipleAccounts=lambda p: dict(value=[None] * len(p[0])))
    assert RC.owner_exclusions(nothing, [wallet]) == ({wallet: None}, None)             # no account, on the curve: a wallet
    assert RC.owner_exclusions(nothing, [pda]) == (None, 'owner_program_unknown')       # no account, off the curve: cannot tell
    assert RC.owner_exclusions(Net(), [wallet])[1] == 'rpc_error'


# ---- cluster policy (pure) ---------------------------------------------------------------------------------------------------------------------------------------------

def decide(balances, funders, **kw):
    traced = []

    def trace(owner, stop):
        traced.append(owner)
        f = funders.get(owner)
        return (f, 'resolved') if f else (None, 'unresolved')
    allowed, reason, metrics = RC.decide_cluster([(o, b) for o, b in balances.items()], trace_fn=trace, **kw)
    return allowed, reason, traced


BOOK = {'o%d' % i: 10 for i in range(8)}      # eight equal owners, 12.5% each


def test_cluster_share_high_at_exactly_25_percent():
    allowed, reason, _ = decide(BOOK, {'o0': 'f', 'o1': 'f', 'o2': 'g', 'o3': 'g', 'o4': 'a', 'o5': 'b', 'o6': 'c', 'o7': 'd'})
    assert (allowed, reason) == (False, 'cluster_share_high')


def test_cluster_top_funder_high_at_exactly_50_percent_and_one_under():
    funders = {o: 'f' + o for o in 'abcde'}      # every owner has its own funder: no shared cluster, so only the top-funder limit can fire
    assert decide({'a': 50, 'b': 12, 'c': 12, 'd': 13, 'e': 13}, funders)[:2] == (False, 'cluster_top_funder_high')
    assert decide({'a': 49, 'b': 12, 'c': 12, 'd': 13, 'e': 14}, funders)[:2] == (True, 'allowed')


def test_every_owner_resolved_with_distinct_funders_is_allowed():
    allowed, reason, traced = decide(BOOK, {o: 'f' + o for o in BOOK})
    assert (allowed, reason) == (True, 'allowed') and sorted(traced) == sorted(BOOK)


def test_an_unresolved_owner_is_unknown_not_allowed():
    book = {'o%d' % i: 10 for i in range(4)}
    allowed, reason, _ = decide(book, {'o0': 'a', 'o1': 'b', 'o2': 'c'})
    assert (allowed, reason) == (False, 'cluster_unknown_unresolved')


def test_a_lone_owner_cannot_form_a_shared_cluster_and_costs_no_trace():
    allowed, reason, traced = decide({'a': 100}, {})
    assert (allowed, reason, traced) == (True, 'pass_thin_bound', [])
    assert decide({}, {})[:2] == (False, 'cluster_no_holders')


def test_deadline_quota_and_trace_errors_end_unknown():
    clock = iter([0., 100.]).__next__
    assert RC.decide_cluster(list(BOOK.items()), trace_fn=lambda o, s: (None, 'unresolved'), deadline_s=5., clock=lambda: clock())[1] == 'cluster_unknown_deadline'

    def quota(o, s):
        raise RC.ClusterQuota('x')

    def boom(o, s):
        raise RuntimeError('x')
    assert RC.decide_cluster(list(BOOK.items()), trace_fn=quota)[1] == 'cluster_unknown_quota'
    assert RC.decide_cluster(list(BOOK.items()), trace_fn=boom)[1] == 'cluster_unknown_trace_error'


# ---- funder trace ------------------------------------------------------------------------------------------------------------------------------------------------------

OWNER, FUNDER = addr(21), addr(22)


def tx(*instructions, err=None):
    return dict(meta=dict(err=err, innerInstructions=[]), transaction=dict(message=dict(instructions=list(instructions))))


def transfer(src, dst, lamports=5):
    return dict(parsed=dict(type='transfer', info=dict(source=src, destination=dst, lamports=lamports)))


def trace_net(pages, transaction):
    it = iter(pages)
    return Net(getSignaturesForAddress=lambda p: next(it), getTransaction=lambda p: transaction)


def test_trace_reads_the_funder_of_the_first_transaction():
    net = trace_net([[dict(signature='new', err=None), dict(signature='first', err=None)]], tx(transfer(FUNDER, OWNER)))
    assert RC.trace_first_funder(net, OWNER) == (FUNDER, 'resolved') and net.calls == ['getSignaturesForAddress', 'getTransaction']


def test_trace_caps_at_two_pages_and_calls_the_history_deep():
    full = [dict(signature='s%d' % i, err=None) for i in range(1000)]
    net = trace_net([full, full, full], tx(transfer(FUNDER, OWNER)))
    assert RC.trace_first_funder(net, OWNER) == (None, 'deep') and net.calls == ['getSignaturesForAddress'] * 2


def test_trace_follows_a_wrapped_sol_close_to_the_payer():
    temp = addr(23)
    ix = [dict(parsed=dict(type='createAccount', info=dict(source=FUNDER, newAccount=temp, lamports=900))), dict(parsed=dict(type='closeAccount', info=dict(account=temp, destination=OWNER)))]
    net = trace_net([[dict(signature='first', err=None)]], tx(*ix))
    assert RC.trace_first_funder(net, OWNER) == (FUNDER, 'resolved')


def test_trace_refuses_a_failed_first_transaction_and_a_failed_read():
    assert RC.trace_first_funder(trace_net([[dict(signature='first', err={'x': 1})]], tx(transfer(FUNDER, OWNER))), OWNER) == (None, 'unresolved')
    assert RC.trace_first_funder(trace_net([[dict(signature='first', err=None)]], tx(transfer(FUNDER, OWNER), err={'x': 1})), OWNER) == (None, 'unresolved')
    with pytest.raises(RuntimeError):
        RC.trace_first_funder(Net(), OWNER)            # a failed page is never "no history"


def test_trace_stops_when_asked():
    net = trace_net([[dict(signature='first', err=None)]], tx(transfer(FUNDER, OWNER)))
    assert RC.trace_first_funder(net, OWNER, should_stop=lambda: True) == (None, 'aborted') and net.calls == []


# ---- through the feed --------------------------------------------------------------------------------------------------------------------------------------------------

def verdict_of(feed):
    (row,) = rows(feed.cfg['desk_db'])
    return row[3:5], json.loads(row[6])['evidence']['checks']


def n_calls(world, method):
    return len([c for c in world.calls if c == ('rpc', method)])


def test_the_default_world_passes_with_lp_burned_and_a_thin_cluster_that_costs_no_trace(rig):
    world, make, t, _ = rig
    feed = make()
    feed.cycle()
    v, checks = verdict_of(feed)
    assert v == ('PASS', 'pass') and checks['lp']['reason'] == 'lp_burned' and checks['cluster']['reason'] == 'pass_thin_bound'
    assert n_calls(world, 'getSignaturesForAddress') == 0 and checks['dump_group']['reason'] == 'not_collected_here'


def test_a_creator_held_lp_on_a_plain_mint_kills_through_the_feed(rig):
    world, make, t, _ = rig
    world.lp_supply = 5
    feed = make()
    feed.cycle()
    assert verdict_of(feed)[0] == ('SKIP', 'kill:lp:lp_held_by_creator')


def test_other_dexes_leave_lp_unknown_and_do_not_block(rig):
    world, make, t, _ = rig
    world.pool_dex = 'raydium'
    feed = make()
    feed.cycle()
    v, checks = verdict_of(feed)
    assert v == ('PASS', 'pass') and (checks['lp']['status'], checks['lp']['reason']) == ('unknown', 'lp_reader_not_available')


def test_wallet_only_top10_at_the_limit_kills_and_the_limit_is_configurable(rig):
    world, make, t, _ = rig
    world.whale = 60
    feed = make()
    feed.cycle()
    assert verdict_of(feed)[0] == ('SKIP', 'kill:holders:top10_concentrated')
    world.whale = 59
    feed = make(desk_db=str(rig[3] / 'b.db'))
    feed.cycle()
    assert verdict_of(feed)[0] == ('PASS', 'pass')
    feed = make(desk_db=str(rig[3] / 'c.db'), holders_top10_kill_pct=50.)
    feed.cycle()
    assert verdict_of(feed)[0] == ('SKIP', 'kill:holders:top10_concentrated')


def test_program_owned_and_burned_balances_are_not_holders_but_stay_in_the_pool_figure(rig):
    world, make, t, _ = rig
    world.extra, world.programs = [('curve', addr(50), 100000), ('burn', RC.INCINERATOR, 50000)], {addr(50): 'SomeProgram'}
    world.whale = 30
    feed = make()
    feed.cycle()
    v, checks = verdict_of(feed)
    assert v == ('PASS', 'pass') and checks['holders']['top10_pct'] == 30. and checks['holders']['top10_incl_pool_pct'] == 95.


def test_an_unclassifiable_owner_makes_holders_unknown_and_skips(rig):
    world, make, t, _ = rig
    world.extra, world.programs = [('x', 'Wal' + '1' * 40, 1)], {'Wal' + '1' * 40: None}      # no account, and not an address
    feed = make()
    feed.cycle()
    assert verdict_of(feed)[0] == ('SKIP', 'unknown:holders:owner_program_unknown')


def book(world, n=6, each=40000, whale=0):
    world.whale = whale
    world.extra = [('e%d' % i, addr(70 + i), each) for i in range(n)]


def test_cluster_is_skipped_when_another_check_kills(rig):
    world, make, t, _ = rig
    book(world)
    world.funder = lambda owner: addr(200)
    world.mint_auth = 'someone'
    feed = make()
    feed.cycle()
    v, checks = verdict_of(feed)
    assert v == ('SKIP', 'kill:token_safety:mint_authority_open') and (checks['cluster']['status'], checks['cluster']['reason']) == ('unknown', 'not_run_other_check_failed')
    assert n_calls(world, 'getSignaturesForAddress') == 0 and n_calls(world, 'getTransaction') == 0


def test_cluster_share_high_kills_through_the_feed(rig):
    world, make, t, _ = rig
    book(world, each=30000)
    world.funder = lambda owner: addr(200)
    feed = make()
    feed.cycle()
    v, checks = verdict_of(feed)
    assert v == ('SKIP', 'kill:cluster:cluster_share_high') and checks['cluster']['clustered_pct'] >= 25.
    assert n_calls(world, 'getSignaturesForAddress') > 0


def test_cluster_top_funder_high_kills_through_the_feed(rig, monkeypatch):
    world, make, t, _ = rig
    book(world, n=3, each=100000, whale=10)
    world.funder = lambda owner: addr(200)
    monkeypatch.setattr(RC, 'CLUSTERED_LIMIT', (101, 100))      # a limit the share cannot reach: only the top-funder limit can fire
    feed = make()
    feed.cycle()
    assert verdict_of(feed)[0] == ('SKIP', 'kill:cluster:cluster_top_funder_high')


def test_a_clean_book_passes_and_an_unresolvable_one_is_unknown_without_blocking(rig):
    world, make, t, _ = rig
    book(world, each=30000)
    world.funder = lambda owner: addr(100 + ord(owner[3]) % 90 + len(owner) % 3)
    feed = make()
    feed.cycle()
    v, checks = verdict_of(feed)
    assert v == ('PASS', 'pass') and checks['cluster']['status'] == 'pass'
    world.funder = lambda owner: None
    feed = make(desk_db=str(rig[3] / 'b.db'))
    feed.cycle()
    v, checks = verdict_of(feed)
    assert v == ('PASS', 'pass') and (checks['cluster']['status'], checks['cluster']['reason']) == ('unknown', 'cluster_unknown_unresolved')


def test_the_cluster_rpc_methods_are_read_only_and_the_known_funder_file_ships():
    assert {'getSignaturesForAddress', 'getTransaction'} <= set(F.RPC_METHODS) and not any(m.startswith(('send', 'request', 'simulate')) for m in F.RPC_METHODS)
    assert RC.KNOWN_FUNDERS_PATH.is_file()
