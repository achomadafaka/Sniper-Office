"""Coin desk: a closed order with a net loss blocks the seat's next paper_order (review_pending) until order_review records a cause and a change."""
import json
import sqlite3

from sniper_office.desk import paper as P
from test_desk_plugin import NOW, desk, ledger, seat, ticket

LOSS = ('lose', 'ep', 'ep', 'm9', 'p9', 'closed', None, NOW - 900, NOW - 880, NOW - 100, 25., 24., 1., 'sl', 'modelled', None)      # 24 - 25 - 1 = -2
WIN = ('win', 'ep', 'ep', 'm8', 'p8', 'closed', None, NOW - 900, NOW - 880, NOW - 100, 25., 30., 1., 'tp', 'modelled', None)
PLAN = dict(reason='why', thesis='t', disproof='d', exit_plan='e')


def env(tmp_path, monkeypatch, orders, role='judge', **cfg):
    c = dict(desk_db=desk(tmp_path / 'd.db', [ticket('a', 5)]), ledger_db=ledger(tmp_path / 'l.db', orders), **cfg)
    return seat(tmp_path, monkeypatch, role=role, cfg=c), c


def j(out):
    return json.loads(out['content'][0]['text'])


def test_loss_blocks_then_order_review_lifts(tmp_path, monkeypatch):
    t, _ = env(tmp_path, monkeypatch, [LOSS])
    d = j(P.paper_order(t, 'a', **PLAN))
    assert d['reason'] == 'review_pending' and d['unreviewed'][0]['order_id'] == 'lose' and d['unreviewed'][0]['net_usd'] == -2. and d['unreviewed'][0]['exit_reason'] == 'sl' and 'order_review' in d['next']
    assert j(P.order_review(t, 'lose', 'stop too tight', ' '))['reason'] == 'cause_and_change_required'
    assert j(P.order_review(t, 'lose', '', 'wider stop'))['reason'] == 'cause_and_change_required'
    assert j(P.order_review(t, 'ghost', 'c', 'd'))['reason'] == 'order_not_in_this_book'
    assert j(P.paper_order(t, 'a', **PLAN))['reason'] == 'review_pending'                 # nothing recorded yet
    assert j(P.order_review(t, 'lose', 'stop too tight', 'wider stop'))['state'] == 'recorded'
    assert j(P.order_review(t, 'lose', 'again', 'again'))['state'] == 'known'              # the first review stands
    assert j(P.paper_order(t, 'a', **PLAN))['state'] == 'queued'
    row = sqlite3.connect(tmp_path / 'l.db').execute('SELECT order_id,role,cause,change FROM order_reviews').fetchall()
    assert row == [('lose', 'judge', 'stop too tight', 'wider stop')]


def test_winner_or_open_order_does_not_block_and_the_gate_has_a_switch(tmp_path, monkeypatch):
    t, _ = env(tmp_path, monkeypatch, [WIN])
    assert j(P.paper_order(t, 'a', **PLAN))['state'] == 'queued'
    (tmp_path / 'x').mkdir()
    t, _ = env(tmp_path / 'x', monkeypatch, [LOSS], review_gate=False)
    assert j(P.paper_order(t, 'a', **PLAN))['state'] == 'queued'


def test_order_review_refuses_open_orders_other_books_and_other_roles(tmp_path, monkeypatch):
    open_order = ('op', 'ep', 'ep', 'm7', 'p7', 'open', None, NOW - 300, NOW - 280, None, 25., None, None, None, None, None)
    foreign = ('fo', 'other', 'other', 'm6', 'p6', 'closed', None, NOW - 900, NOW - 880, NOW - 100, 25., 20., 1., 'sl', 'modelled', None)
    t, cfg = env(tmp_path, monkeypatch, [LOSS, open_order, foreign])
    assert j(P.order_review(t, 'op', 'c', 'd'))['reason'] == 'order_not_closed'
    assert j(P.order_review(t, 'fo', 'c', 'd'))['reason'] == 'order_not_in_this_book'
    assert j(P.order_review(seat(tmp_path, monkeypatch, role='researcher', cfg=cfg), 'lose', 'c', 'd'))['reason'] == 'role_cannot_review'
    assert not sqlite3.connect(tmp_path / 'l.db').execute('SELECT 1 FROM order_reviews').fetchall()
    assert j(P.paper_order(seat(tmp_path, monkeypatch, role='researcher', cfg=cfg), 'a', **PLAN))['reason'] == 'role_cannot_order'     # role check still first
