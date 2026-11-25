"""Entry points of the two BSC creator research collectors, read-only public RPC, no key beyond the endpoint URL.

    python3 -m sniper_office.services.creator.main --config creator.json [collect|venue|anchor]

collect: the first N creation events from one subject's launch venue, with their event windows and quote horizons (was creator20).
venue:   discovery of other known launch venues for the same subject against the collector's checkpoint (was creator-venue).
anchor:  write a fresh start anchor (the current head's header) so a new epoch can start; a collector never re-anchors by itself.

Config: env_file + rpc_url_env (the endpoint URL; its name is the only thing logged), other_env_files (files whose https URLs must not equal the endpoint), state_dir, anchor, seed (collect),
subject + primary_checkpoint (venue), clock_file, quote_input_atomic, max_steps. Exit 2 with one generic line on any invalid input."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from .. import common
from . import _proofs as P
from .collect import run_collection
from .flow import _header
from .rpc import ResearchRpc
from .venue_watch import READ_METHODS as VENUE_READ, _read_json, run_venue_watch
from .watch import MAX_BYTES, READ_METHODS


def _read(path):
    with Path(path).open('rb') as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('input_too_large')
    return json.loads(raw)


def _endpoint(cfg):
    name = cfg['rpc_url_env']
    values = common.env_values(cfg['env_file'])
    others = [p.strip() for f in cfg.get('other_env_files', []) for v in common.env_values(f).values() for p in v.split(',') if p.strip().startswith(('http://', 'https://'))]
    others += [p.strip() for n, v in values.items() if n != name for p in v.split(',') if p.strip().startswith(('http://', 'https://'))]
    return P.isolated_url(values.get(name, ''), others)


def _check_paths(cfg):
    target = Path(cfg['state_dir']).resolve()
    for other in cfg.get('other_state_dirs', []):
        other = Path(other).resolve()
        if target == other or target.is_relative_to(other) or other.is_relative_to(target):
            raise ValueError('research_state_overlap')
    if target.exists() and not target.is_dir():
        raise ValueError('output_not_directory')
    return target


def run(cfg, mode, *, rpc_factory=ResearchRpc):
    rpc = None
    try:
        P.CLOCK_FILE = cfg['clock_file']
        target, url = _check_paths(cfg), _endpoint(cfg)
        max_steps = cfg.get('max_steps')
        if max_steps is not None and (type(max_steps) is not int or max_steps < 0):
            raise ValueError('invalid_steps')
        if mode == 'anchor':
            rpc = rpc_factory(url, allowed=frozenset({'eth_blockNumber', 'eth_getBlockByNumber', 'eth_chainId'}), limit=10)
            if P._quantity(rpc('eth_chainId', [])) != 56:
                raise ValueError('wrong_chain')
            head = P._quantity(rpc('eth_blockNumber', []))
            header = rpc('eth_getBlockByNumber', [hex(head), False])
            _header(header)
            common.write_atomic(cfg['anchor'], dict(version='creator-next20-start-anchor-v1', head=hex(head), header=header))
            print(json.dumps(dict(status='anchor_written', head=head)))
            return 0
        saved = _read(cfg['anchor'])
        anchor = saved.get('header', saved)
        _header(anchor)
        if mode == 'collect':
            seed = _read(cfg['seed'])
            boot = Path('/proc/sys/kernel/random/boot_id')
            session = boot.read_text().strip() if boot.is_file() else str(uuid.uuid4())
            rpc = rpc_factory(url, allowed=READ_METHODS, limit=1_000_000_000)
            state = run_collection(seed, rpc, directory=target, anchor=anchor, quote_input_atomic=str(cfg.get('quote_input_atomic', '1000000000000000')), max_steps=max_steps, clock_session=session)
            print(json.dumps(dict(status=state['status'], reason=state.get('reason'), calls=state.get('calls'), admitted=len(state['launches']), completed=state.get('completed_launches'), directory=str(target))))
        else:
            primary = Path(cfg['primary_checkpoint']).resolve()
            _read_json(primary)
            subject = P._hex(cfg['subject'], 20)
            rpc = rpc_factory(url, allowed=VENUE_READ, limit=1_000_000_000)
            state = run_venue_watch(rpc, directory=target, anchor=anchor, subject=subject, primary_checkpoint=primary, max_steps=max_steps)
            print(json.dumps(dict(status=state['status'], reason=state.get('reason'), calls=state.get('calls'), observations=len(state['observations']), creations=len(state['creations']),
                                  next_block=state['next_block'], directory=str(target))))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        print('creator research: invalid isolated configuration, evidence or research output', file=sys.stderr)
        return 2
    finally:
        if rpc is not None:
            rpc.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('mode', choices=('collect', 'venue', 'anchor'))
    args = parser.parse_args(argv)
    try:
        cfg = common.load_config(args.config)
    except (OSError, ValueError):
        print('creator research: config unreadable', file=sys.stderr)
        return 2
    return run(cfg, args.mode)


if __name__ == '__main__':
    raise SystemExit(main())
