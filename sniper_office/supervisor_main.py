#!/usr/bin/env python3
"""Supervisor process entry point. Your runtime factory supplies the tools, the step function and the policy.

    python3 -m sniper_office.supervisor_main --runtime-factory myproject.office_runtime:build --store office.db --config runtime.json --guard guard.json

The factory is called with store_path, ledger_path, config_path and guard_path and returns a dict:
  supervisor (sniper_office.supervisor.Supervisor), deadline_s, max_steps, tasks() -> [{agent_id, turn_id}],
  and optionally initialize(gen), flush_observations(), observation_health(), close().
It raises RuntimeRefused when the host is not ready; the process then exits 75 so a timer can retry."""
import argparse
from dataclasses import asdict
import importlib
import json
import os
from pathlib import Path
import signal
import threading

from sniper_office.supervisor import StaleGeneration,_put


class RuntimeRefused(ValueError):
    """The host is not ready (guard, clock, caps, pins). Never a crash loop."""


def main(argv=None,*,runtime_factory=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-factory',help='module:function, trusted code')
    parser.add_argument('--store',type=Path,default=Path('office.db'))
    parser.add_argument('--ledger',type=Path,default=None,help='a second database your tools write, if any')
    parser.add_argument('--config',type=Path,default=Path('runtime.json'))
    parser.add_argument('--guard',type=Path,default=Path('guard.json'))
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--poll-s',type=float,default=1.)
    args=parser.parse_args(argv)
    if not 0<args.poll_s<=60: parser.error('poll-s must be in (0,60]')
    if runtime_factory is None:
        module,sep,name=(args.runtime_factory or '').partition(':')
        if not sep or not all(part.isidentifier() for part in module.split('.')) or not name.isidentifier():
            parser.error('--runtime-factory must be module:function')
        try: runtime_factory=getattr(importlib.import_module(module),name)
        except (ImportError,AttributeError):
            print(json.dumps({'state':'refused','reason':'runtime_not_installed'}));return 2
    try:
        runtime=runtime_factory(store_path=args.store,ledger_path=args.ledger,
                                config_path=args.config,guard_path=args.guard)
    except RuntimeRefused as exc:
        print(json.dumps({'state':'paused','reason':str(exc)}),flush=True)
        return 75  # systemd wake timer retries readiness without Restart= churn
    supervisor=runtime['supervisor']
    deadline=runtime['deadline_s'];max_steps=runtime['max_steps']
    if deadline+args.poll_s>=supervisor.lease.ttl_s:
        raise ValueError('lease_must_cover_one_turn_deadline')
    stop=threading.Event()
    previous={}
    gen=None
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig]=signal.signal(sig,lambda *_:stop.set())
    try:
        process_start=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
        gen=supervisor.lease.acquire(f'office-{os.getpid()}-{process_start}')
        supervisor.reconcile(gen=gen)
        if 'initialize' in runtime: runtime['initialize'](gen)
        while not stop.is_set():
            try:
                due=runtime['tasks']()
                supervisor.reconcile_turns(gen=gen)
            except ValueError as exc:
                # Unready host (guard, clock, caps, pins) pauses turns; never a crash loop.
                # Nothing is renewed or written while paused: an unknown clock cannot stamp a lease or heartbeat.
                print(json.dumps({'state':'paused','reason':str(exc)}),flush=True)
                if args.once: break
                stop.wait(args.poll_s);continue
            for task in due:
                if stop.is_set(): break
                supervisor.lease.renew(gen)
                result=supervisor.run_turn(task['agent_id'],gen=gen,deadline_s=deadline,
                                           max_steps=max_steps,turn_id=task['turn_id'])
                if 'flush_observations' in runtime:runtime['flush_observations']()
                print(json.dumps(asdict(result)),flush=True)
            if 'flush_observations' in runtime:runtime['flush_observations']()
            supervisor.lease.renew(gen)
            # The heartbeat is the supervisor's own state; it writes to no other database.
            with supervisor.lease.guard(gen):
                _put(supervisor.conn,'heartbeats','supervisor',
                     {'at':supervisor.clock(),'generation':gen,'frontiers':supervisor.progress_frontiers(),
                      'observations':runtime['observation_health']() if 'observation_health' in runtime else None})
            if args.once: break
            stop.wait(args.poll_s)
        return 0
    except StaleGeneration:
        print(json.dumps({'state':'refused','reason':'stale_supervisor_lease'}),flush=True)
        return 3
    finally:
        if gen is not None:
            try: supervisor.lease.release(gen)
            except (StaleGeneration,ValueError): pass  # unknown clock: the lease expires by itself
        for sig,handler in previous.items(): signal.signal(sig,handler)
        if 'close' in runtime:runtime['close']()


if __name__=='__main__': raise SystemExit(main())
