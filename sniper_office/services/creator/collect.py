"""Restartable, isolated Portal research collection; no execution or ledger access."""
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .flow import _header, _time, analyze_flow_capture, decode_portal_log
from .linked import CREATED, PORTAL, TRADES, analyze_capture
from .watch import MAX_BYTES, READ_METHODS, ZERO, _save, portal_quote_data
from ._proofs import _hex, _quantity
from ._proofs import real_utc_now_iso

MAX_JOB_BYTES = 512 * 1024 * 1024
HORIZONS = (5, 15, 30, 60, 120)
TRANSIENT = {'rate_limit', 'provider_timeout', 'provider_transport', 'provider_http_error'}


class _Failure(Exception):
    def __init__(self, kind, detail=None):
        super().__init__(kind)
        self.detail = dict(detail or {}, kind=kind)


def run_collection(seed, rpc, *, directory, anchor, clock=real_utc_now_iso,
                   monotonic=time.monotonic, sleep=time.sleep,
                   quote_input_atomic='1000000000000000', max_steps=None,
                   poll_seconds=2, clock_session='bootid'):
    """Collect the first twenty post-anchor creations and their complete observation tails.

    Atomic discovery responses precede cursor commits. Replaying an orphan response retains
    its original clocks. An interrupted entry attempt stays unknown and is never repriced.
    """
    validated = analyze_capture(seed)
    anchor_number, _, _, hashes = _header(anchor)
    if len(validated['scenario_members']) != 4 or hashes is None:
        raise ValueError('four_members_and_full_anchor_required')
    if (max_steps is not None and (type(max_steps) is not int or max_steps < 0)
            or isinstance(poll_seconds, bool) or not math.isfinite(float(poll_seconds))
            or not 1 <= float(poll_seconds) <= 60 or not isinstance(clock_session, str) or not clock_session):
        raise ValueError('invalid_collection_bounds')
    if not (type(quote_input_atomic) is int or isinstance(quote_input_atomic,str)
            and quote_input_atomic.isascii() and quote_input_atomic.isdecimal()):
        raise ValueError('invalid_quote_amount')
    amount = int(quote_input_atomic)
    portal_quote_data(ZERO, validated['subject'], amount)
    _time(clock())
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    lock = os.open(root/'collector.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('collection_locked') from None
        return _collect(seed, validated, rpc, root, anchor, anchor_number, amount,
                        clock, monotonic, sleep, max_steps, poll_seconds, clock_session)
    finally:
        os.close(lock)


def _collect(seed, validated, rpc, root, anchor, anchor_number, amount,
             clock, monotonic, sleep, max_steps, poll_seconds, session):
    checkpoint = root/'checkpoint.json'
    identity = dict(seed_digest=hashlib.sha256(json.dumps(seed,sort_keys=True,allow_nan=False).encode()).hexdigest(),
                    anchor=anchor, frozen_members=validated['scenario_members'], quote_input_atomic=str(amount))
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else dict(
        version='creator-flow-collection-v1', **identity, subject=validated['subject'],
        started_at_utc=clock(), discovery_next_block=anchor_number+1, launches=[],
        status='running', reason=None, calls=0, last_error=None, consecutive_failures=0)
    if any(state.get(k) != v for k,v in identity.items()):
        raise ValueError('collection_identity_mismatch')
    if checkpoint.exists(): sleep(1)
    captures = {}
    for item in state['launches']:
        if Path(item['capture']).name != item['capture']: raise ValueError('invalid_capture_path')
        raw = json.loads((root/item['capture']).read_text())
        analyze_flow_capture(raw)
        captures[item['token']] = raw
        if raw['collector']['entry_state'] == 'pending':
            raw['collector'].update(entry_state='unavailable', entry_error='interrupted_entry_attempt')
        for horizon in raw['collector']['horizons'].values():
            if horizon['status'] == 'pending': horizon.update(status='missed', error='interrupted_exit_attempt')
    used_bytes = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
    last_call = None
    response = None

    def save(path, value):
        nonlocal used_bytes
        encoded = json.dumps(value,allow_nan=False).encode()
        if len(encoded) > MAX_BYTES: raise _Failure('capture_too_large')
        # Reserve one capture replacement and the terminal marker for fork invalidation.
        if used_bytes+len(encoded)+MAX_BYTES+65536 > MAX_JOB_BYTES: raise _Failure('job_disk_limit')
        previous = path.stat().st_size if path.exists() else 0
        _save(path,value)
        used_bytes += len(encoded)-previous

    def persist():
        state['updated_at_utc'] = clock()
        state['completed_launches'] = sum(raw['collector']['status']=='complete' for raw in captures.values())
        save(checkpoint,state)

    def publish(token):
        raw = captures[token]
        if (len(raw['transactions']) > 2000 or sum(len(r['logs']) for r in raw['ranges']) > 10000
                or len(raw['quotes']) > 1000): raise _Failure('launch_capture_limit')
        raw['observer'].update(finished_at_utc=clock(), calls=state['calls'])
        analyze_flow_capture(raw)
        save(root/next(r['capture'] for r in state['launches'] if r['token']==token),raw)

    def read(method, params, *, missing=False):
        nonlocal last_call, response
        if method not in READ_METHODS: raise _Failure('read_method_refused')
        wait = max(0, last_call+1-monotonic()) if last_call is not None else 0
        if wait: sleep(wait)
        last_call = monotonic()
        response = dict(request_at_utc=clock(), request_monotonic=last_call, clock_session=session)
        state['last_call_at_utc'] = response['request_at_utc']
        state['calls'] += 1
        try:
            result = rpc(method,params)
            if result is None and not missing: raise _Failure('missing_result')
            return result
        except _Failure: raise
        except Exception as exc:
            detail = deepcopy(getattr(rpc,'last_error',None) or {})
            kind = detail.get('kind') or (str(exc) if str(exc) in TRANSIENT | {
                'provider_rejected','provider_redirect','malformed_response','rpc_rejected',
                'credits_exhausted','missing_result','budget'} else 'provider_transport')
            if missing and kind == 'missing_result': return None
            raise _Failure(kind,detail) from None
        finally:
            response.update(response_at_utc=clock(), response_monotonic=monotonic())

    def header(number, previous=None):
        raw = read('eth_getBlockByNumber',[hex(number),False])
        parsed = _header(raw)
        if parsed[0] != number or parsed[3] is None: raise _Failure('malformed_header')
        if previous is not None and parsed != _header(previous): raise _Failure('anchor_reorg')
        if parsed[2] > _time(clock()): raise _Failure('future_header')
        return raw

    def remember(raw, block):
        existing = next((h for h in raw['headers'] if h['number']==block['number']),None)
        if existing is not None and _header(existing) != _header(block): raise _Failure('anchor_reorg')
        if existing is None: raw['headers'].append(deepcopy(block))

    def query(lo, hi, topics, *, discovery=False):
        record=root/f'discovery-{lo:012d}.json'
        if discovery and record.exists():
            row=json.loads(record.read_text())
        else:
            terminal=header(hi)
            filt=dict(address=PORTAL,topics=[topics])
            logs=read('eth_getLogs',[dict(filt,fromBlock=hex(lo),toBlock=hex(hi))])
            if not isinstance(logs,list): raise _Failure('malformed_logs')
            row=dict(from_block=lo,to_block=hi,filter=filt,logs=logs,
                observed_at_utc=response['response_at_utc'],observed_monotonic=response['response_monotonic'],
                clock_session=session,observation_mode='forward',terminal_header=terminal)
            if len(logs)>10000: raise _Failure('response_log_limit')
            if discovery: save(record,row)
        if (row['from_block']!=lo or row['filter']!=dict(address=PORTAL,topics=[topics])
                or row['observation_mode']!='forward'): raise _Failure('conflicting_discovery_records')
        for item in row['logs']:
            event=decode_portal_log(item)
            if event is None or not lo<=event['order'][0]<=row['to_block'] or item['topics'][0].lower() not in topics:
                raise _Failure('malformed_logs')
        header(row['to_block'],row['terminal_header'])
        return row

    def preview(token, kind, horizon=None):
        raw=captures[token]; bookkeeping=raw['collector']
        prior=next((q for q in raw['quotes'] if q['kind']=='entry' and q['status']=='ok'),None)
        if kind=='entry': bookkeeping['entry_state']='pending'
        else: bookkeeping['horizons'][str(horizon)]['status']='pending'
        publish(token)
        try: at=header(_quantity(read('eth_blockNumber',[])))
        except _Failure as exc:
            if kind=='entry': bookkeeping.update(entry_state='unavailable',entry_error=str(exc))
            else: bookkeeping['horizons'][str(horizon)].update(status='missed',error=str(exc))
            publish(token)
            raise
        if prior is not None and _quantity(at['number'])<=_quantity(prior['header']['number']):
            bookkeeping['horizons'][str(horizon)].update(status='missed',error='no_later_quote_block')
            publish(token); return
        remember(raw,at)
        quantity=amount if kind=='entry' else int(prior['expected_output_atomic'])
        source,target=(ZERO,token) if kind=='entry' else (token,ZERO)
        row=dict(id=str(len(raw['quotes'])+1),kind=kind,token=token,input_token=source,output_token=target,
            amount_atomic=str(quantity),calldata=portal_quote_data(source,target,quantity),header=at,
            quantity_basis='fixed_research_native_input' if kind=='entry' else 'entry_quote_expected_not_fill',
            observation_mode='forward',status='unavailable',error='interrupted_quote',result=None,
            clock_session=session)
        if prior is not None: row.update(entry_quote_id=prior['id'],horizon_seconds=horizon)
        failure=None
        try:
            row['result']=read('eth_call',[dict(to=PORTAL,data=row['calldata']),at['number']],missing=True)
            if row['result'] is None: row['error']='quote_missing_result'
            else:
                _hex(row['result'],32)
                output=int(row['result'],16)
                if output<=0: row['error']='nonpositive_quote'
                else: row.update(status='ok',error=None,expected_output_atomic=str(output))
        except (ValueError,TypeError):
            row['error']='malformed_provider_evidence';failure=_Failure('malformed_provider_evidence')
        except _Failure as exc:
            row['error']=str(exc);row['provider_error']=exc.detail
            if str(exc)!='contract_revert': failure=exc
        row.update(response)
        if row['status']=='ok':
            try: header(_quantity(at['number']),at)
            except (_Failure,ValueError,TypeError,KeyError,IndexError) as exc:
                failure=exc if isinstance(exc,_Failure) else _Failure('malformed_provider_evidence')
                row.update(status='unavailable',error=str(failure),expected_output_atomic=None)
        raw['quotes'].append(row)
        if kind=='entry':
            bookkeeping['entry_state']='ok' if row['status']=='ok' else 'unavailable'
            if row['status']=='ok':
                bookkeeping['horizons']={str(h):dict(due_at_epoch=_time(row['response_at_utc'])+h,status='scheduled') for h in HORIZONS}
        else: bookkeeping['horizons'][str(horizon)].update(status=row['status'],quote_id=row['id'])
        publish(token)
        if failure is not None: raise failure

    def discover(head):
        lo=state['discovery_next_block']
        if len(state['launches'])==20 or lo>head: return
        row=query(lo,min(head,lo+255),[CREATED],discovery=True)
        facts=sorted((decode_portal_log(e) for e in row['logs']),key=lambda e:e['order'])
        creation_headers={_quantity(row['terminal_header']['number']):row['terminal_header']}
        for event in facts:
            if event['actor']!=state['subject'] or any(r['token']==event['token'] for r in state['launches']): continue
            if len(state['launches'])==20: break
            ident={k:event[k] for k in ('token','tx_hash','order')}
            filename='launch-'+event['token']+'.json'
            detection=dict(ident,detected_at_utc=row['observed_at_utc'],detected_monotonic=row['observed_monotonic'],
                clock_session=row['clock_session'],detection_basis='eth_getLogs_response',anchor_block=anchor_number,
                observation_mode='forward',startup_backlog=event['timestamp']<_time(state['started_at_utc']))
            raw=dict(version='creator-flow-capture-v1',chain='56',subject=state['subject'],
                frozen_members=validated['scenario_members'],window_seconds=120,seed_capture=deepcopy(seed),
                headers=[deepcopy(anchor),deepcopy(row['terminal_header'])],ranges=[deepcopy(row)],
                transactions=[],quotes=[],gaps=[],selection=dict(mode='forward',launch_limit=20,admitted_launches=[ident]),
                observer=dict(started_at_utc=state['started_at_utc'],start_anchor=deepcopy(anchor),start_block=anchor_number+1,
                    launch_detections=[detection],clock_provenance='corrected_utc_and_session_monotonic',
                    funded_authority=False,ownership_proven=False,reorg_detected=False),
                collector=dict(next_block=event['order'][0],creation_timestamp=event['timestamp'],
                    entry_state='not_started',horizons={},status='active'))
            captures[event['token']]=raw
            state['launches'].append(dict(ident,capture=filename))
            try:
                if event['order'][0] not in creation_headers:
                    creation_headers[event['order'][0]]=header(event['order'][0])
                remember(raw,creation_headers[event['order'][0]])
                publish(event['token'])
            except BaseException:
                state['launches'].pop();del captures[event['token']]
                raise
        state['discovery_next_block']=row['to_block']+1
        state['frontier_header']=row['terminal_header']
        persist()

    def cover(head):
        active=[raw for raw in captures.values() if raw['collector']['status']!='complete'
                and not raw['collector'].get('coverage_complete') and raw['collector']['next_block']<=head]
        if not active: return
        lo=min(raw['collector']['next_block'] for raw in active)
        row=query(lo,min(head,lo+255),[CREATED,*TRADES])
        if any(sum(len(r['logs']) for r in raw['ranges'])+len(row['logs'])>10000 for raw in active):
            raise _Failure('launch_capture_limit')
        facts=[decode_portal_log(e) for e in row['logs']]
        cohort={raw['selection']['admitted_launches'][0]['token'] for raw in active}
        blocks={f['order'][0] for f in facts if f['token'] in cohort}
        bound={number:header(number) for number in sorted(blocks)}
        bundles=[]
        for number,block in bound.items():
            receipts=read('eth_getBlockReceipts',[hex(number)],missing=True)
            if receipts is not None and not isinstance(receipts,list): raise _Failure('malformed_receipts')
            by_hash={_hex(r['transactionHash'],32):r for r in receipts or []}
            if len(by_hash)!=len(receipts or []): raise _Failure('duplicate_receipt')
            for event in (f for f in facts if f['order'][0]==number and f['token'] in cohort):
                if any(b.get('event_hash')==event['tx_hash'] for b in bundles): continue
                index=event['order'][1]
                if index>=len(block['transactions']) or block['transactions'][index].lower()!=event['tx_hash']:
                    raise _Failure('log_transaction_not_in_header')
                tx=read('eth_getTransactionByBlockHashAndIndex',[block['hash'],hex(index)],missing=True)
                receipt=by_hash.get(event['tx_hash'])
                item=dict(transaction=tx,receipt=receipt,header=block,event_hash=event['tx_hash'])
                if tx is None or receipt is None: item['acquisition_failure']='transaction_missing' if tx is None else 'receipt_missing'
                bundles.append(item)
        for raw in active:
            token=raw['selection']['admitted_launches'][0]['token']
            relevant={f['tx_hash'] for f in facts if f['token']==token}
            existing={b.get('event_hash') for b in raw['transactions']}
            raw['transactions'] += [deepcopy(b) for b in bundles if b['event_hash'] in relevant-existing]
            raw['ranges'].append(deepcopy(row))
            for block in [row['terminal_header'],*bound.values()]: remember(raw,block)
            raw['collector']['next_block']=row['to_block']+1
            raw['collector']['frontier_header']=row['terminal_header']
            report=analyze_flow_capture(raw)
            raw['collector']['coverage_complete']=report['launches'][0]['coverage']['state']=='provider_complete'
            publish(token)

    try:
        persist()
        if state['status'] in ('blocked','complete'): return state
        step=0;verified=False
        while max_steps is None or step<max_steps:
            step+=1
            try:
                if not verified:
                    if _quantity(read('eth_chainId',[]))!=56: raise _Failure('wrong_chain')
                    for raw in captures.values():
                        if raw['collector'].get('frontier_header'):
                            at=raw['collector']['frontier_header'];header(_quantity(at['number']),at)
                    verified=True
                header(anchor_number,anchor)
                if state.get('frontier_header'):
                    at=state['frontier_header'];header(_quantity(at['number']),at)
                frontiers={(_quantity(raw['collector']['frontier_header']['number']),
                            raw['collector']['frontier_header']['hash']):raw['collector']['frontier_header']
                    for raw in captures.values() if raw['collector'].get('frontier_header')}
                for (number,_),at in frontiers.items(): header(number,at)
                head=_quantity(read('eth_blockNumber',[]))
                discover(head)
                for token,raw in captures.items():
                    if raw['collector']['entry_state']=='not_started': preview(token,'entry')
                for token,raw in captures.items():
                    for horizon,job in raw['collector']['horizons'].items():
                        if job['status']!='scheduled' or _time(clock())<job['due_at_epoch']: continue
                        if _time(clock())>job['due_at_epoch']+max(5,float(poll_seconds)*2):
                            job.update(status='missed',error='quote_horizon_missed');publish(token)
                        else: preview(token,'exit',int(horizon))
                cover(head)
                for token,raw in captures.items():
                    if (raw['collector'].get('coverage_complete') and raw['collector']['entry_state'] in ('ok','unavailable')
                            and all(j['status'] not in ('scheduled','pending') for j in raw['collector']['horizons'].values())):
                        raw['collector']['status']='complete';publish(token)
                state.update(last_error=None,consecutive_failures=0,reason=None)
                if len(state['launches'])==20 and all(raw['collector']['status']=='complete' for raw in captures.values()):
                    state.update(status='complete',reason='twenty_launch_windows_complete')
                persist()
                if state['status']=='complete': break
                sleep(float(poll_seconds))
            except _Failure as exc:
                state['last_error']=exc.detail
                if str(exc) not in TRANSIENT: raise
                state['consecutive_failures']+=1
                state['reason']='provider_backoff'
                persist()
                # Sleep in short pieces so the isolated process remains interruptible.
                for _ in range(min(10,2**min(state['consecutive_failures']-1,4))): sleep(60)
        if state['status']=='running': state['reason']='step_limit'
        persist()
    except (_Failure,ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError) as exc:
        reason=str(exc) if isinstance(exc,_Failure) else 'malformed_provider_evidence'
        if reason=='anchor_reorg':
            for item in state['launches']:
                raw=captures[item['token']]
                raw['observer']['reorg_detected']=True
                raw['gaps'].append(dict(from_block=item['order'][0],
                    to_block=max(r['to_block'] for r in raw['ranges']),reason=reason))
                raw['collector']['status']='invalidated'
                _save(root/item['capture'],raw)
            state['completed_launches']=0
        state.update(status='blocked',reason=reason,last_error=exc.detail if isinstance(exc,_Failure) else dict(kind=reason))
        state['updated_at_utc']=clock()
        # Keep the terminal marker even if the configured data budget has been exhausted.
        _save(checkpoint,state)
    return state
