"""Finite Portal observations on one injected read-only RPC; no executor or database."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time

from ._abi import encode
from ._abi import keccak

from .linked import MAX_BYTES, PORTAL, CREATED, TRADES, analyze_capture
from ._proofs import _hex, _quantity
from ._proofs import real_utc_now_iso

ZERO = '0x'+'0'*40
READ_METHODS = frozenset({'eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_getLogs',
    'eth_getTransactionByBlockHashAndIndex','eth_getBlockReceipts','eth_call'})
ERRORS = frozenset({'rate_limit','credits_exhausted','provider_rejected','provider_redirect',
    'provider_http_error','provider_timeout','provider_transport','malformed_response',
    'rpc_rejected','missing_result','budget','rate_limit_pause'})


def portal_quote_data(input_token, output_token, amount):
    """Official IPortal quoteExactInput(QuoteExactInputParams), three static words.

    https://github.com/flap-sh/FlapVaultExample/blob/main/src/flap/IPortal.sol
    This is a quote, never swap calldata, a floor, or a funded transaction.
    """
    input_token,output_token=_hex(input_token,20),_hex(output_token,20)
    if type(amount) is not int or not 0<amount<2**256:
        raise ValueError('invalid_quote_amount')
    selector=keccak(text='quoteExactInput((address,address,uint256))')[:4]
    return '0x'+(selector+encode(['(address,address,uint256)'],[(input_token,output_token,amount)])).hex()


def _utc(value):
    parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('aware_clock_required')
    return str(value)


def _save(path, capture):
    raw=json.dumps(capture,allow_nan=False).encode()
    if len(raw)>MAX_BYTES:raise ValueError('capture_too_large')
    fd,temporary=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as handle:
            handle.write(raw);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


class _Stop(Exception):
    pass


def run_watch(seed_capture,rpc,*,output_path,max_seconds=180,max_calls=120,
              clock=real_utc_now_iso,monotonic=time.monotonic,sleep=time.sleep,
              quote_input_atomic='1000000000000000',from_block=None,to_block=None,poll_seconds=2):
    """Capture finite historical ranges or new blocks after a saved head anchor.

    The supplied RPC owns endpoint isolation. No endpoint or exception text enters the artifact.
    Exact block-number calls are bracketed by header hash checks; no hidden fallback is used.
    """
    validated=analyze_capture(seed_capture)
    if len(validated['scenario_members'])!=4:raise ValueError('four_frozen_members_required')
    if type(max_calls) is not int or not 1<=max_calls<=1000:raise ValueError('invalid_call_limit')
    if isinstance(max_seconds,bool) or not 0<float(max_seconds)<=172800:raise ValueError('invalid_duration')
    historical=from_block is not None or to_block is not None
    if historical and (type(from_block) is not int or type(to_block) is not int or not 0<=from_block<=to_block):
        raise ValueError('invalid_historical_range')
    if not 1<=float(poll_seconds)<=60:raise ValueError('invalid_poll_period')
    if quote_input_atomic is not None and not (type(quote_input_atomic) is int or
            isinstance(quote_input_atomic,str) and quote_input_atomic.isascii() and quote_input_atomic.isdecimal()):
        raise ValueError('invalid_quote_amount')
    amount=None if quote_input_atomic is None else int(quote_input_atomic)
    if amount is not None:portal_quote_data(ZERO,validated['subject'],amount)
    path=Path(output_path)
    if not path.parent.is_dir():raise ValueError('output_parent_missing')
    from .flow import decode_portal_log
    mode='historical' if historical else 'forward'
    start=monotonic();deadline=start+float(max_seconds)
    observer=dict(started_at_utc=_utc(clock()),max_seconds=float(max_seconds),max_calls=max_calls,
        requests=[],launch_detections=[],clock_provenance='injected_corrected_utc_and_same_process_monotonic',
        funded_authority=False,ownership_proven=False,reorg_detected=False)
    out=dict(version='creator-flow-capture-v1',chain='56',subject=validated['subject'],
        frozen_members=validated['scenario_members'],window_seconds=120,seed_capture=deepcopy(seed_capture),
        headers=[],ranges=[],transactions=[],quotes=[],gaps=[],
        selection=dict(mode=mode,launch_limit=20,from_block=from_block,to_block=to_block),observer=observer)
    headers,seen_transactions,launches,entries,exited={},{},{},{},set()
    next_block=None;last_call=None;active_range=None;anchor=None

    def read(method,params,*,missing=False):
        nonlocal last_call
        if method not in READ_METHODS:raise _Stop('read_method_refused')
        if len(observer['requests'])>=max_calls:raise _Stop('call_limit')
        if last_call is not None:
            wait=max(0.0,last_call+1.0-monotonic())
            if monotonic()+wait>=deadline:raise _Stop('duration_limit')
            if wait:sleep(wait)
        if monotonic()>=deadline:raise _Stop('duration_limit')
        record=dict(method=method,params=deepcopy(params),request_at_utc=_utc(clock()),
                    request_monotonic=monotonic(),status='ok',error=None)
        last_call=record['request_monotonic'];observer['requests'].append(record)
        try:
            result=rpc(method,params)
            if result is None and not missing:raise ValueError('missing_result')
            return result
        except Exception as exc:
            reason=str(exc) if str(exc) in ERRORS else 'provider_timeout' if isinstance(exc,TimeoutError) else 'provider_transport'
            record.update(status='unavailable',error=reason)
            if missing and reason=='missing_result':return None
            raise _Stop(reason) from None
        finally:
            record.update(response_at_utc=_utc(clock()),response_monotonic=monotonic())

    def header(number,*,fresh=False):
        if not fresh and number in headers:return headers[number]
        raw=read('eth_getBlockByNumber',[hex(number),False])
        if (not isinstance(raw,dict) or _quantity(raw['number'])!=number
                or not isinstance(raw.get('transactions'),list)):
            raise _Stop('malformed_header')
        _hex(raw['hash'],32);_quantity(raw['timestamp'])
        hashes=[_hex(h,32) for h in raw['transactions']]
        if len(hashes)!=len(set(hashes)):raise _Stop('duplicate_header_transaction')
        previous=headers.get(number)
        if previous and (previous['hash']!=raw['hash'] or previous['timestamp']!=raw['timestamp']
                         or previous['transactions']!=raw['transactions']):
            observer.update(reorg_detected=True,changed_anchor_header=raw)
            raise _Stop('anchor_reorg')
        if previous is None:headers[number]=raw;out['headers'].append(raw)
        return raw

    def quote(kind,token,at,entry=None):
        quantity=amount if entry is None else int(entry['expected_output_atomic'])
        source,target=(ZERO,token) if kind=='entry' else (token,ZERO)
        row=dict(id=str(len(out['quotes'])+1),kind=kind,token=token,input_token=source,output_token=target,
            amount_atomic=str(quantity),calldata=portal_quote_data(source,target,quantity),header=at,
            quantity_basis='fixed_research_native_input' if entry is None else 'entry_quote_expected_not_fill',
            observation_mode=mode,status='unavailable',error=None,result=None)
        if entry is not None:row['entry_quote_id']=entry['id']
        out['quotes'].append(row)
        prior_count=len(observer['requests'])
        try:
            row['result']=read('eth_call',[dict(to=PORTAL,data=row['calldata']),at['number']],missing=True)
            _hex(row['result'],32)
            value=int(row['result'],16)
            if value<=0:raise ValueError('nonpositive_quote')
            row.update(status='ok',expected_output_atomic=str(value))
        except (ValueError,TypeError):row['error']='quote_unavailable_or_malformed'
        except _Stop as exc:
            row['error']=str(exc);raise
        finally:
            if len(observer['requests'])>prior_count:
                call=observer['requests'][-1]
                row.update({k:call[k] for k in ('request_at_utc','response_at_utc','request_monotonic','response_monotonic')})
            else:out['quotes'].pop()
        if row['status']=='ok':
            try:header(_quantity(at['number']),fresh=True)
            except (_Stop,ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError) as exc:
                reason=str(exc) if isinstance(exc,_Stop) else 'malformed_provider_evidence'
                row.update(status='unavailable',error=reason,expected_output_atomic=None)
                raise _Stop(reason) from None
        return row

    try:
        if _quantity(read('eth_chainId',[]))!=56:raise _Stop('wrong_chain')
        head=_quantity(read('eth_blockNumber',[]));anchor=header(head)
        next_block=from_block if historical else head+1
        observer.update(start_anchor=anchor,start_block=next_block)
        if historical and to_block>head:raise _Stop('historical_end_after_anchor')
        while True:
            if monotonic()>=deadline:raise _Stop('duration_limit')
            if not historical:head=_quantity(read('eth_blockNumber',[]))
            end=min(to_block if historical else head,next_block+9)
            if next_block<=end:
                active_range=(next_block,end)
                flt=dict(address=PORTAL,topics=[[CREATED,*TRADES]])
                logs=read('eth_getLogs',[dict(flt,fromBlock=hex(next_block),toBlock=hex(end))])
                response=observer['requests'][-1]
                observed_at=response['response_at_utc'];observed_mono=response['response_monotonic']
                if not isinstance(logs,list):raise _Stop('malformed_logs')
                if sum(len(r['logs']) for r in out['ranges'])+len(logs)>10000:raise _Stop('log_limit')
                terminal=header(end)
                row=dict(from_block=next_block,to_block=end,terminal_header=terminal,filter=flt,logs=logs,
                         observed_at_utc=observed_at,observation_mode=mode)
                out['ranges'].append(row)
                facts=[]
                for item in logs:
                    event=decode_portal_log(item)
                    if event is not None:
                        if not next_block<=event['order'][0]<=end:raise _Stop('log_outside_range')
                        facts.append(event)
                for event in sorted(facts,key=lambda f:f['order']):
                    if event['kind']=='create' and event['actor']==out['subject']:
                        if event['token'] not in launches:
                            observer['launch_detections'].append(dict(token=event['token'],tx_hash=event['tx_hash'],
                                order=event['order'],detected_at_utc=observed_at,detected_monotonic=observed_mono,
                                detection_basis='eth_getLogs_response',anchor_block=_quantity(anchor['number']),
                                observation_mode=mode))
                        launches.setdefault(event['token'],event)
                if len(launches)>20:raise _Stop('launch_limit')
                tokens=set(launches)
                if historical:tokens.update(e['token'] for e in validated['creator_events'] if e['creator']==out['subject'])
                blocks={f['order'][0] for f in facts if f['token'] in tokens}
                for number in sorted(blocks):
                    block=header(number)
                    receipts=read('eth_getBlockReceipts',[hex(number)],missing=True)
                    if receipts is not None and not isinstance(receipts,list):raise _Stop('malformed_receipts')
                    bound={}
                    for receipt in receipts or []:
                        signature=_hex(receipt['transactionHash'],32)
                        if signature in bound:raise _Stop('duplicate_block_receipt')
                        bound[signature]=receipt
                    for event in (f for f in facts if f['order'][0]==number and f['token'] in tokens):
                        signature=event['tx_hash']
                        if signature in seen_transactions:continue
                        if len(seen_transactions)>=2000:raise _Stop('transaction_limit')
                        index=event['order'][1]
                        if index>=len(block['transactions']) or block['transactions'][index].lower()!=signature:
                            raise _Stop('log_transaction_not_in_header')
                        tx=read('eth_getTransactionByBlockHashAndIndex',[block['hash'],hex(index)],missing=True)
                        receipt=bound.get(signature)
                        bundle=dict(transaction=tx,receipt=receipt,header=block)
                        if tx is None:bundle['acquisition_failure']='transaction_missing'
                        elif _hex(tx['hash'],32)!=signature:raise _Stop('transaction_index_mismatch')
                        if receipt is None:bundle['acquisition_failure']='receipt_missing'
                        out['transactions'].append(bundle);seen_transactions[signature]=bundle
                active_range=None;next_block=end+1
                if amount is not None:
                    for token in sorted(launches):
                        if token not in entries:
                            entry=quote('entry',token,terminal)
                            if entry['status']=='ok':entries[token]=entry
                        elif token not in exited and _quantity(terminal['number'])>_quantity(entries[token]['header']['number']):
                            quote('exit',token,terminal,entries[token]);exited.add(token)
                header(_quantity(anchor['number']),fresh=True)
                if historical and next_block>to_block:raise _Stop('historical_range_complete')
            elif historical:raise _Stop('historical_range_complete')
            if len(launches)==20:raise _Stop('launch_limit')
            if not historical:
                sleep(min(float(poll_seconds),max(0,deadline-monotonic())))
    except _Stop as exc:
        observer['stop_reason']=str(exc)
        if str(exc)=='anchor_reorg' and out['ranges']:
            out['gaps'].append(dict(from_block=min(r['from_block'] for r in out['ranges']),
                to_block=max(r['to_block'] for r in out['ranges']),reason='anchor_reorg'))
        elif active_range is not None:
            out['gaps'].append(dict(from_block=active_range[0],to_block=active_range[1],reason=str(exc)))
    except (ValueError,TypeError,KeyError,IndexError,AttributeError):
        observer['stop_reason']='malformed_provider_evidence'
        if active_range is not None:
            out['gaps'].append(dict(from_block=active_range[0],to_block=active_range[1],reason='malformed_provider_evidence'))
    observer.update(finished_at_utc=_utc(clock()),elapsed_seconds=monotonic()-start,calls=len(observer['requests']))
    _save(path,out)
    return out
