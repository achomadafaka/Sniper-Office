"""Isolated BSC launch discovery; no quotes, trades, database or funded authority."""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from ._abi import DecodingError
from ._abi import keccak

from .flow import _header, _time, decode_portal_log
from .linked import CREATED, PORTAL, _abi, _atomic, _bytes
from .watch import MAX_BYTES, _save
from ._proofs import _hex, _quantity
from ._proofs import real_utc_now_iso

FOUR = '0x5c952063c7fc8610ffdb798152d69f0b9550762b'
FOUR_CREATED = '0x'+keccak(text='TokenCreate(address,address,uint256,string,string,uint256,uint256,uint256)').hex()
STAGED = '0x'+keccak(text='FlapTokenStaged(uint256,address,address)').hex()
VANITY = '0x'+keccak(text='VanityTokenCreated(address,address,address)').hex()
FILTER = dict(address=[PORTAL,FOUR],topics=[[CREATED,FOUR_CREATED,STAGED,VANITY]])
READ_METHODS = frozenset({'eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_getLogs'})
MAX_JOB_BYTES = 512*1024*1024
TRANSIENT = {'rate_limit','provider_timeout','provider_transport','provider_http_error','credits_exhausted'}


def decode_venue_log(log):
    """Bind official emitter/topic/ABI pairs to their event creator, never tx sender."""
    try:
        address=_hex(log['address'],20)
        topics=[_hex(t,32) for t in log['topics']]
        if len(topics)!=1 or log.get('removed',False) is not False:
            raise ValueError('invalid_venue_topics_or_removed')
        topic=topics[0]
        if (address,topic)==(PORTAL,CREATED):
            row=decode_portal_log(log)
            row.update(venue='flap',event_kind='TokenCreated')
        else:
            if (address,topic)==(FOUR,FOUR_CREATED):
                words=_abi(['address','address','uint256','string','string','uint256','uint256','uint256'],_bytes(log['data']))
                actor,token=words[:2]
                row=dict(venue='four',kind='create',event_kind='TokenCreate',request_id=str(words[2]),
                    name=words[3],symbol=words[4],total_supply_atomic=str(words[5]),
                    launch_time=words[6],launch_fee_atomic=str(words[7]))
            elif (address,topic)==(PORTAL,STAGED):
                words=_abi(['uint256','address','address'],_bytes(log['data']))
                _,actor,token=words
                row=dict(venue='flap',kind='staged',event_kind='FlapTokenStaged',event_timestamp=words[0])
            elif (address,topic)==(PORTAL,VANITY):
                words=_abi(['address','address','address'],_bytes(log['data']))
                token,actor,beneficiary=words
                row=dict(venue='flap',kind='vanity',event_kind='VanityTokenCreated',beneficiary=beneficiary.lower())
            else:
                raise ValueError('unsupported_emitter_event_pair')
            row.update(actor=actor.lower(),creator=actor.lower(),token=token.lower(),
                tx_hash=_hex(log['transactionHash'],32),
                order=[_atomic(log[k]) for k in ('blockNumber','transactionIndex','logIndex')])
        row.update(block_hash=_hex(log['blockHash'],32),emitter=address,topic=topic)
        if not row['order'][0]:raise ValueError('invalid_event_block')
        row['event_id']=row['tx_hash']+':'+str(row['order'][2])
        return row
    except (KeyError,TypeError,AttributeError,IndexError,DecodingError,OverflowError):
        raise ValueError('malformed_venue_log') from None


class _Failure(Exception):
    pass


def _read_json(path):
    with Path(path).open('rb') as handle:raw=handle.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('capture_too_large')
    return json.loads(raw)


def run_venue_watch(rpc,*,directory,anchor,subject,primary_checkpoint,clock=real_utc_now_iso,
                    monotonic=time.monotonic,sleep=time.sleep,max_steps=None):
    """Journal known venue responses before committing a frozen subject/anchor cursor."""
    number,_,_,hashes=_header(anchor);subject=_hex(subject,20);_time(clock())
    if hashes is None or max_steps is not None and (type(max_steps) is not int or max_steps<0):
        raise ValueError('invalid_watch_bounds_or_anchor')
    root=Path(directory).resolve();primary=Path(primary_checkpoint).resolve()
    if root==primary.parent or root.is_relative_to(primary.parent) or primary.parent.is_relative_to(root):
        raise ValueError('watch_overlaps_primary_state')
    root.mkdir(parents=True,exist_ok=True)
    lock=os.open(root/'venue-watch.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('venue_watch_locked') from None
        return _watch(rpc,root,anchor,number,subject,primary,clock,monotonic,sleep,max_steps)
    finally:os.close(lock)


def _watch(rpc,root,anchor,anchor_number,subject,primary,clock,monotonic,sleep,max_steps):
    checkpoint=root/'checkpoint.json'
    primary_initial=_read_json(primary)
    primary_identity={k:primary_initial[k] for k in ('version','seed_digest','anchor','subject')}
    identity=dict(version='creator-venue-watch-v1',chain='56',anchor=anchor,subject=subject,primary_checkpoint=str(primary),
        primary_identity=primary_identity,abi_version='creator-venue-abis-v1',filter=deepcopy(FILTER))
    state=_read_json(checkpoint) if checkpoint.exists() else dict(
        **identity,started_at_utc=clock(),
        next_block=anchor_number+1,setup_head=None,observations=[],creations=[],
        funded_authority=False,joins_primary_cohort=False,evidence_valid=True,
        status='running',reason=None,calls=0,last_error=None,consecutive_failures=0)
    if any(state.get(k)!=v for k,v in identity.items()):raise ValueError('watch_identity_mismatch')
    last_call=None;response=None;session=str(uuid.uuid4())
    if checkpoint.exists():sleep(1)

    def size(value):return len(json.dumps(value,allow_nan=False).encode())

    def save(path,value,*,terminal=False):
        encoded=size(value)
        file_reserve=65536 if path==checkpoint and not terminal else 0
        if encoded+file_reserve>MAX_BYTES:raise _Failure('capture_too_large')
        used=sum(p.stat().st_size for p in root.iterdir() if p.is_file())
        # Reserve a full terminal checkpoint replacement plus its bounded status growth.
        reserve=0 if terminal else size(state)+65536
        if used+encoded+reserve>MAX_JOB_BYTES:raise _Failure('job_disk_limit')
        _save(path,value)

    def persist(value=None):
        candidate=deepcopy(state if value is None else value)
        candidate['updated_at_utc']=clock()
        save(checkpoint,candidate)
        state.update(candidate)

    def read(method,params):
        nonlocal last_call,response
        if method not in READ_METHODS:raise _Failure('read_method_refused')
        if last_call is not None:
            wait=max(0,last_call+1-monotonic())
            if wait:sleep(wait)
        last_call=monotonic();state['calls']+=1
        response=dict(request_at_utc=clock(),request_monotonic=last_call)
        try:
            result=rpc(method,params)
            if result is None:raise _Failure('missing_result')
            return result
        except _Failure:raise
        except Exception as exc:
            kind=(getattr(rpc,'last_error',None) or {}).get('kind')
            if not kind:
                kind=str(exc) if str(exc) in TRANSIENT|{'provider_rejected','provider_redirect',
                    'malformed_response','rpc_rejected','missing_result','budget'} else 'provider_timeout' if isinstance(exc,TimeoutError) else 'provider_transport'
            raise _Failure(kind) from None
        finally:response.update(response_at_utc=clock(),response_monotonic=monotonic())

    def header(number,previous=None):
        raw=read('eth_getBlockByNumber',[hex(number),False]);parsed=_header(raw)
        if parsed[0]!=number or parsed[3] is None:raise _Failure('malformed_header')
        if previous is not None and parsed!=_header(previous):raise _Failure('anchor_reorg')
        if parsed[2]>_time(clock()):raise _Failure('future_header')
        return raw

    def primary_frontier():
        row=_read_json(primary)
        if (row['subject']!=subject or row['anchor']!=anchor
                or any(row.get(k)!=v for k,v in primary_identity.items())):
            raise _Failure('primary_identity_mismatch')
        if row['status'] not in ('running','blocked','complete'):raise _Failure('invalid_primary_status')
        frontier=_quantity(row['discovery_next_block'])-1
        if row.get('frontier_header'):frontier=max(frontier,_header(row['frontier_header'])[0])
        if frontier<anchor_number:raise _Failure('invalid_primary_frontier')
        if row['status']=='complete':
            launches=row.get('launches',[])
            if not isinstance(launches,list) or len(launches)>20:raise _Failure('invalid_primary_captures')
            seen=set()
            for launch in launches:
                name=launch['capture']
                if (not isinstance(name,str) or name in ('','.','..') or Path(name).name!=name
                        or name in seen or (primary.parent/name).resolve().parent!=primary.parent):
                    raise _Failure('invalid_primary_capture_path')
                seen.add(name)
                capture=_read_json(primary.parent/name)
                frontier=max(frontier,_header(capture['collector']['frontier_header'])[0])
        return row['status']=='complete',frontier

    def discover(head):
        lo=state['next_block'];hi=min(head,lo+255)
        record=root/f'discovery-{lo:012d}.json'
        if record.exists():row=_read_json(record)
        else:
            terminal=header(hi)
            logs=read('eth_getLogs',[dict(deepcopy(FILTER),fromBlock=hex(lo),toBlock=hex(hi))])
            if not isinstance(logs,list):raise _Failure('malformed_logs')
            if len(logs)>10000:raise _Failure('response_log_limit')
            row=dict(from_block=lo,to_block=hi,filter=deepcopy(FILTER),logs=logs,terminal_header=terminal,
                observed_at_utc=response['response_at_utc'],observed_monotonic=response['response_monotonic'],
                setup_head=state['setup_head'],clock_session=session)
            save(record,row)
        if (row['from_block']!=lo or type(row['to_block']) is not int or not lo<=row['to_block']<=min(head,lo+255)
                or row['filter']!=FILTER or row['setup_head']!=state['setup_head']
                or not isinstance(row['logs'],list) or len(row['logs'])>10000):
            raise _Failure('conflicting_discovery_records')
        if (_time(row['observed_at_utc'])>_time(clock())
                or not isinstance(row['clock_session'],str) or not row['clock_session']
                or type(row['observed_monotonic']) not in (float,int)
                or not 0<=row['observed_monotonic']<float('inf')):
            raise _Failure('invalid_detection_clock')
        facts=[];seen={};block_indices=set()
        for raw in row['logs']:
            event=decode_venue_log(raw)
            if not lo<=event['order'][0]<=row['to_block']:raise _Failure('log_outside_range')
            block_index=(event['order'][0],event['order'][2])
            if event['event_id'] in seen or block_index in block_indices:
                raise _Failure('duplicate_or_conflicting_log')
            block_indices.add(block_index)
            seen[event['event_id']]=raw;facts.append(event)
        bound={row['to_block']:row['terminal_header']}
        for event in sorted(facts,key=lambda e:e['order']):
            if event['actor']!=subject:continue
            number,index,_=event['order']
            if number not in bound:bound[number]=header(number)
            _,block_hash,at,hashes=_header(bound[number])
            if (block_hash!=event['block_hash'] or index>=len(hashes) or hashes[index]!=event['tx_hash']):
                raise _Failure('log_header_identity_mismatch')
            event.update(chain_timestamp=at,header=bound[number],detected_at_utc=row['observed_at_utc'],
                detected_monotonic=row['observed_monotonic'],detection_basis='eth_getLogs_response',
                clock_session=row['clock_session'],
                startup_backlog=number<=row['setup_head'],
                observation_mode='setup_backlog' if number<=row['setup_head'] else 'forward')
        header(row['to_block'],row['terminal_header'])
        header(anchor_number,anchor)
        candidate=deepcopy(state)
        existing={e['event_id']:e for e in candidate['observations']}
        creations={(e['venue'],e['token']):e for e in candidate['creations']}
        for event in facts:
            if event['actor']!=subject:continue
            prior=existing.get(event['event_id'])
            if prior is not None:
                if prior!=event:raise _Failure('conflicting_observation')
                continue
            candidate['observations'].append(event)
            if event['kind']=='create':
                key=(event['venue'],event['token'])
                if key not in creations:
                    item=dict(venue=key[0],token=key[1],event_ids=[])
                    creations[key]=item;candidate['creations'].append(item)
                creations[key]['event_ids'].append(event['event_id'])
        candidate.update(next_block=row['to_block']+1,frontier_header=row['terminal_header'])
        persist(candidate)

    try:
        persist()
        if state['status'] in ('blocked','complete'):return state
        verified=False;step=0
        while max_steps is None or step<max_steps:
            step+=1
            try:
                if not verified:
                    if _quantity(read('eth_chainId',[]))!=56:raise _Failure('wrong_chain')
                    verified=True
                header(anchor_number,anchor)
                if state.get('frontier_header'):
                    at=state['frontier_header'];header(_quantity(at['number']),at)
                complete,frontier=primary_frontier()
                if complete and state['next_block']>frontier:
                    state.update(status='complete',reason='primary_complete_and_drained');persist();break
                head=_quantity(read('eth_blockNumber',[]))
                if head<anchor_number:raise _Failure('head_before_anchor')
                if state['setup_head'] is None:state['setup_head']=head;persist()
                if complete:head=min(head,frontier)
                if state['next_block']<=head:discover(head)
                state.update(last_error=None,consecutive_failures=0,reason=None)
                if complete and state['next_block']>frontier:
                    state.update(status='complete',reason='primary_complete_and_drained')
                persist()
                if state['status']=='complete':break
                sleep(2)
            except _Failure as exc:
                state['last_error']=dict(kind=str(exc))
                if str(exc) not in TRANSIENT:raise
                state['consecutive_failures']+=1;state['reason']='provider_backoff';persist()
                for _ in range(min(10,2**min(state['consecutive_failures']-1,4))):sleep(60)
        if state['status']=='running':state['reason']='step_limit'
        persist()
    except (_Failure,OSError,ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError) as exc:
        reason=str(exc) if isinstance(exc,_Failure) else 'malformed_provider_evidence'
        state.update(status='blocked',reason=reason,last_error=dict(kind=reason),updated_at_utc=clock())
        if reason=='anchor_reorg':state['evidence_valid']=False
        save(checkpoint,state,terminal=True)
    return state
