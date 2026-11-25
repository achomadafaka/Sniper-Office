"""The BSC creator research collectors against a fake chain: no network, no key."""
from datetime import datetime, timezone
import json
import time

import pytest

from sniper_office.services.creator import _proofs as P, main as CM, venue_watch as VW

SUBJECT = '0x' + '44' * 20
NOW = time.time()


def iso():
    return datetime.fromtimestamp(NOW + 100, tz=timezone.utc).isoformat()      # a fixed corrected clock a little after every block time


def hdr(n, salt=''):
    return dict(number=hex(n), hash='0x' + ('%064x' % n if not salt else (salt * 64)[:64]), timestamp=hex(int(NOW) - 1000 + n % 1000), transactions=[])


class Chain:
    """A fake JSON-RPC node: head = anchor + 6, no logs, optional reorg of the anchor block."""
    def __init__(self, anchor, reorg=False):
        self.anchor, self.reorg, self.calls = anchor, reorg, []
        self.last_error = {}

    def __call__(self, method, params):
        self.calls.append(method)
        if method == 'eth_chainId':
            return '0x38'
        if method == 'eth_blockNumber':
            return hex(self.anchor + 6)
        if method == 'eth_getBlockByNumber':
            n = int(params[0], 16)
            return hdr(n, 'ee') if self.reorg and n == self.anchor else hdr(n)
        if method == 'eth_getLogs':
            return []
        raise AssertionError(method)

    def close(self):
        pass


def setup(tmp_path, n=1_000):
    anchor = hdr(n)
    primary = tmp_path / 'primary' / 'checkpoint.json'
    primary.parent.mkdir()
    primary.write_text(json.dumps(dict(version='creator-flow-collection-v1', seed_digest='d', anchor=anchor, subject=SUBJECT, status='running', discovery_next_block=n + 1)))
    return anchor, primary


def watch(tmp_path, anchor, primary, rpc, steps=2):
    return VW.run_venue_watch(rpc, directory=tmp_path / 'venue', anchor=anchor, subject=SUBJECT, primary_checkpoint=primary, clock=iso, monotonic=iter(range(10**6)).__next__, sleep=lambda s: None, max_steps=steps)


def test_venue_watch_scans_forward_from_the_anchor_in_bounded_steps_and_records_what_it_read(tmp_path):
    anchor, primary = setup(tmp_path)
    chain = Chain(1_000)
    state = watch(tmp_path, anchor, primary, chain)
    assert state['status'] == 'running' and state['reason'] == 'step_limit' and state['next_block'] == 1_007 and state['setup_head'] == 1_006 and state['observations'] == [] and state['funded_authority'] is False
    assert chain.calls.count('eth_getLogs') == 1 and (tmp_path / 'venue' / 'discovery-000000001001.json').exists() and state['calls'] == len(chain.calls)
    again = watch(tmp_path, anchor, primary, Chain(1_000), steps=1)         # a restart resumes the same cursor
    assert again['next_block'] == 1_007 and again['started_at_utc'] == state['started_at_utc']


def test_venue_watch_a_reorged_anchor_blocks_and_invalidates_the_evidence(tmp_path):
    anchor, primary = setup(tmp_path)
    state = watch(tmp_path, anchor, primary, Chain(1_000, reorg=True))
    assert state['status'] == 'blocked' and state['reason'] == 'anchor_reorg' and state['evidence_valid'] is False


def test_venue_watch_refuses_a_primary_checkpoint_that_is_not_the_one_it_was_started_against(tmp_path):
    anchor, primary = setup(tmp_path)
    watch(tmp_path, anchor, primary, Chain(1_000), steps=1)
    doc = json.loads(primary.read_text())
    primary.write_text(json.dumps(dict(doc, subject='0x' + '55' * 20)))
    with pytest.raises(ValueError, match='watch_identity_mismatch'):
        watch(tmp_path, anchor, primary, Chain(1_000), steps=1)


def test_venue_watch_refuses_overlapping_state_and_a_second_instance(tmp_path):
    anchor, primary = setup(tmp_path)
    with pytest.raises(ValueError, match='watch_overlaps_primary_state'):
        VW.run_venue_watch(Chain(1_000), directory=primary.parent, anchor=anchor, subject=SUBJECT, primary_checkpoint=primary, clock=iso, max_steps=0)


def cfg(tmp_path, **kw):
    env, other = tmp_path / 'r.env', tmp_path / 'other.env'
    env.write_text('RPC_URL=https://rpc.example/key-secret-123\nOTHER=1\n')
    other.write_text('SOMETHING=https://elsewhere.example/x\n')
    clock = tmp_path / 'clock.json'
    clock.write_text(json.dumps(dict(offset_s=0., at=time.time())))
    return dict(dict(env_file=str(env), rpc_url_env='RPC_URL', other_env_files=[str(other)], state_dir=str(tmp_path / 'state'), anchor=str(tmp_path / 'anchor.json'), clock_file=str(clock)), **kw)


def test_anchor_mode_writes_the_current_head_header_and_prints_no_endpoint(tmp_path, capsys):
    made = []

    def factory(url, **kw):
        made.append(url)
        return Chain(5_000)
    assert CM.run(cfg(tmp_path), 'anchor', rpc_factory=factory) == 0
    saved = json.loads((tmp_path / 'anchor.json').read_text())
    assert saved['version'] == 'creator-next20-start-anchor-v1' and saved['head'] == hex(5_006) and saved['header']['number'] == hex(5_006) and made == ['https://rpc.example/key-secret-123']
    assert 'key-secret' not in capsys.readouterr().out


def test_entry_point_refuses_a_shared_endpoint_a_plain_http_one_and_overlapping_state_without_echoing_the_url(tmp_path, capsys):
    shared = cfg(tmp_path)
    (tmp_path / 'other.env').write_text('SAME=https://rpc.example/key-secret-123\n')
    assert CM.run(shared, 'anchor', rpc_factory=lambda *a, **k: Chain(1)) == 2
    plain = cfg(tmp_path)
    (tmp_path / 'r.env').write_text('RPC_URL=http://rpc.example/key-secret-123\n')
    assert CM.run(plain, 'anchor', rpc_factory=lambda *a, **k: Chain(1)) == 2
    (tmp_path / 'r.env').write_text('RPC_URL=https://rpc.example/key-secret-123\n')
    (tmp_path / 'other.env').write_text('X=1\n')
    assert CM.run(cfg(tmp_path, other_state_dirs=[str(tmp_path)]), 'anchor', rpc_factory=lambda *a, **k: Chain(1)) == 2
    err = capsys.readouterr().err
    assert err.count('invalid isolated configuration') == 3 and 'key-secret' not in err
    assert CM.run(cfg(tmp_path), 'collect', rpc_factory=lambda *a, **k: Chain(1)) == 2          # no anchor file yet


def test_the_clock_fails_closed_without_a_fresh_correction_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(P, 'CLOCK_FILE', None)
    with pytest.raises(ValueError, match='clock_unknown'):
        P.real_utc_now_iso()
    stale = tmp_path / 'c.json'
    stale.write_text(json.dumps(dict(offset_s=0., at=time.time() - 7200)))
    with pytest.raises(ValueError, match='clock_unknown'):
        P.real_utc_now_iso(str(stale))
    fresh = tmp_path / 'f.json'
    fresh.write_text(json.dumps(dict(offset_s=0., at=time.time())))
    assert P.real_utc_now_iso(str(fresh)).endswith('+00:00')


def test_read_transport_checks_the_envelope_and_never_follows_a_redirect():
    class R:
        def __init__(self, status, doc):
            self.status_code, self.doc = status, doc

        def json(self):
            return self.doc
    def rpc(post, method='eth_blockNumber'):
        return P.ReadRpc('https://x', post=lambda url, **kw: post(kw), allowed=frozenset({'eth_blockNumber'}))(method, [])
    assert rpc(lambda kw: R(200, dict(jsonrpc='2.0', id=1, result='0x1'))) == '0x1'
    for status, doc, reason in [(302, {}, 'provider_redirect'), (429, {}, 'rate_limit'), (403, {}, 'provider_rejected'), (500, {}, 'provider_http_error'), (200, dict(jsonrpc='2.0', id=9, result='0x1'), 'malformed_response'),
                                (200, dict(jsonrpc='2.0', id=1, error='rate limit exceeded'), 'rate_limit'), (200, dict(jsonrpc='2.0', id=1, result=None), 'missing_result')]:
        with pytest.raises(ValueError, match=reason):
            rpc(lambda kw, s=status, d=doc: R(s, d))
    with pytest.raises(ValueError, match='read_method_refused'):
        rpc(lambda kw: R(200, {}), method='eth_sendRawTransaction')
