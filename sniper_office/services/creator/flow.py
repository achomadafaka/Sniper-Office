"""Pure, bounded Portal flow census. Event actors are not inferred owners or fills."""
from datetime import datetime
import json
import math

from ._abi import DecodingError

from .linked import (
    MAX_BYTES, PORTAL, CREATED, TRADES, CREATED_TYPES, TRADE_TYPES,
    _abi, _atomic, _bytes, analyze_capture,
)
from ._proofs import _hex, _mined, _quantity

VERSION = 'creator-flow-capture-v1'
ZERO = '0x' + '0' * 40
LIMITS = [
    'Successful Portal events from supplied provider responses only; failed and other-venue coverage unknown.',
    'Outside the frozen seed set does not prove independent ownership.',
    'Header/receipt consistency is not independent finality or receipt-root verification.',
    'First and last sales are observed sales; final inventory is unknown.',
    'Passive previews are expected output, not floors, fills, landing evidence or counterfactual profit.',
]


def decode_portal_log(log):
    """Decode official canonical Portal events without equating actor and tx initiator."""
    try:
        if _hex(log['address'], 20) != PORTAL:
            return None
        topics = [_hex(t, 32) for t in log['topics']]
        if not topics or topics[0] not in {CREATED, *TRADES}:
            return None
        if len(topics) != 1 or log.get('removed', False) is not False:
            raise ValueError('invalid_portal_topics_or_removed_log')
        create = topics[0] == CREATED
        words = _abi(CREATED_TYPES if create else TRADE_TYPES, _bytes(log['data']))
        actor, token = (words[1], words[3]) if create else (words[2], words[1])
        row = dict(kind='create' if create else TRADES[topics[0]], actor=actor.lower(),
            address=actor.lower(), token=token.lower(), timestamp=words[0],
            tx_hash=_hex(log['transactionHash'], 32),
            order=[_atomic(log[k]) for k in ('blockNumber', 'transactionIndex', 'logIndex')])
        _hex(log['blockHash'], 32)
        if not row['timestamp'] or not row['order'][0]:
            raise ValueError('invalid_event_clock')
        if create:
            row.update(creator=actor.lower(), nonce=str(words[2]))
        else:
            row.update(token_amount_atomic=str(words[3]), quote_amount_raw=str(words[4]),
                fee_amount_raw=str(words[5]), post_price_raw=str(words[6]), quote_asset=None,
                quote_units='unknown_quote_asset_atomic')
        return row
    except (KeyError, TypeError, AttributeError, IndexError, DecodingError, OverflowError) as exc:
        raise ValueError('malformed_portal_log: ' + str(exc)) from None


def _time(value):
    at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('aware_clock_required')
    return at.timestamp()


def _header(raw):
    number, at = _atomic(raw['number']), _atomic(raw['timestamp'])
    if not number or not at:
        raise ValueError('invalid_header')
    hashes = raw.get('transactions')
    if hashes is not None:
        if not isinstance(hashes, list):
            raise ValueError('invalid_header_transactions')
        hashes = [_hex(v['hash'] if isinstance(v, dict) else v, 32) for v in hashes]
        if len(hashes) != len(set(hashes)):
            raise ValueError('duplicate_header_transaction')
    return number, _hex(raw['hash'], 32), at, hashes


def _quotes(raw, headers):
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError('invalid_quote_count')
    rows, entries, seen = [], {}, set()
    for quote in raw:
        row = dict(quote)
        key = row['id']
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError('invalid_quote_id')
        seen.add(key)
        kind, token = row['kind'], _hex(row['token'], 20)
        input_token, output_token = _hex(row['input_token'], 20), _hex(row['output_token'], 20)
        amount = _atomic(int(row['amount_atomic']))
        if not amount or kind not in ('entry', 'exit'):
            raise ValueError('invalid_quote_quantity_or_kind')
        expected_assets = (ZERO, token) if kind == 'entry' else (token, ZERO)
        if (input_token, output_token) != expected_assets:
            raise ValueError('quote_asset_mismatch')
        basis = 'fixed_research_native_input' if kind == 'entry' else 'entry_quote_expected_not_fill'
        if row['quantity_basis'] != basis:
            raise ValueError('invalid_quote_quantity_basis')
        data = _bytes(row['calldata'])
        if data[:4].hex() != 'fc847c2b' or _abi(['(address,address,uint256)'], data[4:])[0] != (
                input_token, output_token, amount):
            raise ValueError('quote_calldata_mismatch')
        start, end = _time(row['request_at_utc']), _time(row['response_at_utc'])
        clocks = [row[k] for k in ('request_monotonic', 'response_monotonic')]
        if (end < start or any(type(c) not in (int, float) or not math.isfinite(c) or c < 0 for c in clocks)
                or clocks[1] < clocks[0] or row['observation_mode'] not in ('forward', 'historical')):
            raise ValueError('invalid_quote_clocks')
        session = row.get('clock_session')
        if 'clock_session' in row and (not isinstance(session,str) or not 1<=len(session)<=128):
            raise ValueError('invalid_quote_clock_session')
        quote_header = _header(row['header']) if row.get('header') is not None else None
        if quote_header and quote_header[2] > start:
            raise ValueError('quote_header_after_request')
        if kind == 'exit':
            previous = entries.get(row['entry_quote_id'])
            previous_session = previous.get('clock_session') if previous else None
            if (session is None) != (previous_session is None):
                raise ValueError('mixed_quote_clock_sessions')
            same_session = session == previous_session
            if (previous is None or previous['token'] != token or previous['status'] != 'ok'
                    or amount != int(previous['expected_output_atomic'])
                    or start < _time(previous['response_at_utc'])
                    or same_session and clocks[0] < previous['response_monotonic']
                    or quote_header is None or quote_header[0] <= _atomic(previous['header']['number'])):
                raise ValueError('unbound_exit_quote_quantity')
            row['clock_comparison'] = 'utc_and_monotonic' if same_session else 'utc_across_sessions'
        if row['status'] == 'ok':
            if row.get('error') is not None:
                raise ValueError('successful_quote_has_error')
            output = _abi(['uint256'], _bytes(row['result']))[0]
            if not output or str(output) != row['expected_output_atomic']:
                raise ValueError('quote_result_mismatch')
            if quote_header is None:
                raise ValueError('successful_quote_header_missing')
            number, blockhash, at, _ = quote_header
            if number in headers and headers[number][1:3] != (blockhash, at):
                raise ValueError('quote_header_conflict')
            headers.setdefault(number, _header(row['header']))
            row['expected_output_atomic'] = str(output)
        elif row['status'] == 'unavailable':
            if not row.get('error') or row.get('expected_output_atomic') is not None:
                raise ValueError('failed_quote_requires_unknown_output')
            row['expected_output_atomic'] = None
        else:
            raise ValueError('unsupported_quote_status')
        row.update(token=token, input_token=input_token, output_token=output_token, amount_atomic=str(amount),
                   is_fill=False, landing_proven=False, native_net_cash=None)
        rows.append(row)
        if kind == 'entry':
            entries[key] = row
    return rows


def analyze_flow_capture(capture):
    try:
        return _analyze(capture)
    except (KeyError, TypeError, AttributeError, IndexError, DecodingError, OverflowError, RecursionError) as exc:
        raise ValueError('malformed_flow_capture: ' + str(exc)) from None


def _analyze(capture):
    if (not isinstance(capture, dict) or capture.get('version') != VERSION or capture.get('chain') != '56'
            or type(capture.get('window_seconds')) is not int or capture['window_seconds'] != 120):
        raise ValueError('unsupported_flow_capture')
    if len(json.dumps(capture, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError('flow_capture_too_large')
    seed = analyze_capture(capture['seed_capture'])
    subject = _hex(capture['subject'], 20)
    members = [_hex(a, 20) for a in capture['frozen_members']]
    if (subject != seed['subject'] or len(members) != 4 or len(set(members)) != 4
            or set(members) != set(seed['scenario_members'])):
        raise ValueError('frozen_seed_mismatch')
    for key in ('headers', 'ranges', 'transactions', 'gaps'):
        if not isinstance(capture[key], list):
            raise ValueError('invalid_' + key)
    if len(capture['transactions']) > 2000 or sum(len(r['logs']) for r in capture['ranges']) > 10000:
        raise ValueError('flow_capture_limit')
    selection = capture['selection']
    if not isinstance(selection, (dict, str)):
        raise ValueError('invalid_selection')
    mode = selection.get('mode', 'historical') if isinstance(selection, dict) else 'historical'
    if mode not in ('historical', 'forward') or not isinstance(capture['observer'], dict):
        raise ValueError('invalid_observation_mode')
    headers = {}
    def register(raw):
        row = _header(raw); old = headers.get(row[0])
        if old and (old[1:3] != row[1:3] or old[3] is not None and row[3] is not None and old[3] != row[3]):
            raise ValueError('conflicting_header')
        headers[row[0]] = row if not old or row[3] is not None else old
        return row
    for raw in capture['headers']:
        register(raw)
    start_block = 0
    if mode == 'forward':
        anchor = register(capture['observer']['start_anchor'])
        start_block = _atomic(capture['observer']['start_block'])
        if start_block != anchor[0] + 1:
            raise ValueError('invalid_forward_start')
    ranges, raw_logs = [], []
    for query in capture['ranges']:
        first, last = _atomic(query['from_block']), _atomic(query['to_block'])
        terminal = register(query['terminal_header'])
        if first > last or terminal[0] != last or not isinstance(query['logs'], list):
            raise ValueError('invalid_query_interval')
        _time(query['observed_at_utc'])
        if query['observation_mode'] not in ('historical', 'forward'):
            raise ValueError('invalid_query_mode')
        filt = query['filter']; topics = filt['topics']
        if _hex(filt['address'], 20) != PORTAL or not isinstance(topics, list) or len(topics) != 1:
            raise ValueError('unsupported_query_filter')
        if any(k in filt and _atomic(filt[k]) != expected for k, expected in (('fromBlock', first), ('toBlock', last))):
            raise ValueError('query_filter_interval_mismatch')
        allowed = {_hex(t, 32) for t in (topics[0] if isinstance(topics[0], list) else [topics[0]])}
        if not allowed or not allowed.issubset({CREATED, *TRADES}):
            raise ValueError('unsupported_query_topics')
        keys = set()
        for log in query['logs']:
            event = decode_portal_log(log)
            if event is None or log['topics'][0].lower() not in allowed or not first <= event['order'][0] <= last:
                raise ValueError('query_response_filter_mismatch')
            keys.add((event['order'][0], event['order'][2]))
            raw_logs.append(log)
        complete_filter = set(TRADES).issubset(allowed)
        ranges.append(dict(first=first,last=last,at=terminal[2],keys=keys,allowed=allowed,
            eligible=complete_filter and query.get('status', 'ok') == 'ok',mode=query['observation_mode'],
            observed_at_utc=query['observed_at_utc']))
    for item in [*capture['seed_capture']['transactions'], *capture['transactions']]:
        for log in (item['receipt']['logs'] if item['receipt'] is not None else []):
            if decode_portal_log(log) is not None:
                raw_logs.append(log)
    events, raw_by_key, orders, tx_orders = {}, {}, {}, {}
    for log in raw_logs:
        event = decode_portal_log(log); block, index, logindex = event['order']
        key = (block, logindex)
        identity = (event, _hex(log['blockHash'], 32))
        if key in events and identity != (events[key], _hex(raw_by_key[key]['blockHash'], 32)):
            raise ValueError('conflicting_event_duplicate')
        previous = orders.setdefault((block, index), event['tx_hash'])
        tx_identity = (_hex(log['blockHash'], 32), block, index)
        if previous != event['tx_hash'] or tx_orders.setdefault(event['tx_hash'], tx_identity) != tx_identity:
            raise ValueError('conflicting_transaction_order')
        events[key], raw_by_key[key] = event, log
    observations, receipt_logs, bodies = {}, {}, {}
    for item in [*capture['seed_capture']['transactions'], *capture['transactions']]:
        tx, receipt, raw_header = item['transaction'], item['receipt'], item['header']
        number, blockhash, at, hashes = _header(raw_header)
        if number in headers and headers[number][1:3] != (blockhash, at):
            raise ValueError('transaction_header_conflict')
        if receipt is not None:
            # Receipt metadata can refute an event even when its transaction body is unavailable.
            metadata = {key:receipt[source] for key,source in (
                ('hash','transactionHash'),('blockHash','blockHash'),('blockNumber','blockNumber'),
                ('transactionIndex','transactionIndex'),('from','from'),('to','to'))}
            _mined(receipt,metadata,raw_header,'56',allow_failed=True)
            receipt_index = _atomic(receipt['transactionIndex'])
            if hashes is not None and (receipt_index>=len(hashes) or hashes[receipt_index]!=metadata['hash'].lower()):
                raise ValueError('receipt_header_transaction_mismatch')
            if _quantity(receipt['status'])==0 and receipt['logs']:
                raise ValueError('failed_receipt_has_logs')
            receipt_logs[metadata['hash'].lower()] = {
                (r['order'][0],r['order'][2]):r for raw in receipt['logs']
                if (r := decode_portal_log(raw)) is not None}
        if tx is None:
            if not item.get('acquisition_failure'):
                raise ValueError('missing_transaction_without_failure')
            continue
        signature = _hex(tx['hash'], 32); index = _atomic(tx['transactionIndex'])
        sender, target = _hex(tx['from'], 20), _hex(tx['to'], 20)
        body = (sender,target,number,index,blockhash,_atomic(tx['value']),_bytes(tx['input']))
        if bodies.setdefault(signature,body) != body:
            raise ValueError('conflicting_transaction_body')
        if (_atomic(tx['chainId']) != 56 or _atomic(tx['blockNumber']) != number
                or _hex(tx['blockHash'], 32) != blockhash
                or hashes is not None and (index >= len(hashes) or hashes[index] != signature)):
            raise ValueError('transaction_identity_mismatch')
        if (orders.setdefault((number,index),signature) != signature
                or tx_orders.setdefault(signature,(blockhash,number,index)) != (blockhash,number,index)):
            raise ValueError('conflicting_transaction_order')
        gas = success = None
        if receipt is None:
            if not item.get('acquisition_failure'):
                raise ValueError('missing_receipt_without_failure')
        else:
            _mined(receipt,tx,raw_header,'56',allow_failed=True)
            success = _quantity(receipt['status']) == 1
            if not success and receipt['logs']:
                raise ValueError('failed_receipt_has_logs')
            gas = str(_atomic(receipt['gasUsed']) * _atomic(receipt['effectiveGasPrice']))
        row = dict(tx_hash=signature,initiator=sender,target=target,order=[number,index],
            signed_native_value_atomic=str(_atomic(tx['value'])), gas_paid_atomic=gas,
            receipt_state='bound' if receipt is not None else 'unavailable',success=success)
        old = observations.get(signature)
        if old and old != row:
            unknown = {'gas_paid_atomic','receipt_state','success'}
            if {k:v for k,v in old.items() if k not in unknown} != {k:v for k,v in row.items() if k not in unknown}:
                raise ValueError('conflicting_transaction_bundle')
            if old['receipt_state']=='bound':
                if row['receipt_state']=='bound':raise ValueError('conflicting_transaction_receipt')
                continue
        observations[signature] = row
    creations = sorted((r for r in events.values() if r['kind']=='create' and r['actor']==subject
                        and r['order'][0] >= start_block),key=lambda r:r['order'])
    admitted = selection.get('admitted_launches') if isinstance(selection,dict) else None
    if admitted is not None:
        if mode!='forward' or not isinstance(admitted,list) or not 1<=len(admitted)<=20:
            raise ValueError('invalid_shard_admission')
        identities = [(_hex(r['token'],20),_hex(r['tx_hash'],32),tuple(_atomic(v) for v in r['order'])) for r in admitted]
        available = {(r['token'],r['tx_hash'],tuple(r['order'])) for r in creations}
        if (any(len(r[2])!=3 for r in identities) or len(set(identities))!=len(identities)
                or identities!=sorted(identities,key=lambda r:r[2]) or not set(identities).issubset(available)):
            raise ValueError('shard_creation_identity_mismatch')
        creations = [r for r in creations if (r['token'],r['tx_hash'],tuple(r['order'])) in identities]
    if len(creations) > 20 or len({r['token'] for r in creations}) != len(creations):
        raise ValueError('invalid_launch_count')
    detections = {}
    for row in capture['observer'].get('launch_detections', []):
        key = (_hex(row['token'],20),_hex(row['tx_hash'],32),tuple(_atomic(v) for v in row['order']))
        if key in detections or len(key[2])!=3 or row['detection_basis']!='eth_getLogs_response':
            raise ValueError('invalid_launch_detection')
        _time(row['detected_at_utc'])
        mono = row['detected_monotonic']
        if type(mono) not in (int,float) or not math.isfinite(mono) or mono<0 or row['observation_mode']!=mode:
            raise ValueError('invalid_launch_detection_clock')
        detections[key] = row
    if not set(detections).issubset((r['token'],r['tx_hash'],tuple(r['order'])) for r in creations):
        raise ValueError('detection_creation_mismatch')
    tracked = {r['token'] for r in creations}
    bound = {}
    for key, event in events.items():
        if event['token'] not in tracked:
            continue
        block, index, _ = event['order']; header = headers.get(block); raw = raw_by_key[key]
        header_bound = header is not None and header[3] is not None
        if header is not None and (_hex(raw['blockHash'],32) != header[1] or event['timestamp'] != header[2]):
            raise ValueError('event_header_mismatch')
        if header_bound and (index >= len(header[3]) or header[3][index] != event['tx_hash']):
            raise ValueError('event_header_transaction_mismatch')
        tx = observations.get(event['tx_hash']); receipt = receipt_logs.get(event['tx_hash'])
        if receipt is not None and receipt.get(key) != event:
            raise ValueError('query_receipt_event_mismatch')
        initiator = tx['initiator'] if tx else None
        bound[key] = dict(event,header_bound=header_bound,initiator=initiator,
            attribution='initiator_unknown' if initiator is None else 'event_actor_matches_initiator'
                if initiator==event['actor'] else 'event_actor_differs_from_initiator',
            gas_paid_atomic=tx['gas_paid_atomic'] if tx else None,
            receipt_state=tx['receipt_state'] if tx else 'unavailable')
    quotes = _quotes(capture['quotes'], headers)
    gaps = []
    for gap in capture['gaps']:
        first,last = _atomic(gap['from_block']),_atomic(gap['to_block'])
        if first>last or not isinstance(gap['reason'],str) or not gap['reason']:
            raise ValueError('invalid_gap')
        gaps.append((first,last))
    launches = []
    for creation in creations:
        token, start = creation['token'],creation['order'][0]
        end_at = creation['timestamp'] + 120
        rows = sorted((r for r in bound.values() if r['token']==token and r['order']>=creation['order']
                       and r['timestamp']<=end_at),key=lambda r:r['order'])
        creation_row = next(r for r in rows if r['kind']=='create')
        key = (creation['order'][0],creation['order'][2])
        sources = [q for q in ranges if q['mode']=='forward' and key in q['keys']] if mode=='forward' else []
        detected = min(sources,key=lambda q:_time(q['observed_at_utc']))['observed_at_utc'] if sources else None
        detection = detections.get((token,creation['tx_hash'],tuple(creation['order'])))
        if detection and mode=='forward' and (detected is None or
                _time(detection['detected_at_utc'])!=_time(detected) or detection['anchor_block']!=start_block-1):
            raise ValueError('detection_response_clock_mismatch')
        latency = _time(detected)-creation['timestamp'] if detected else None
        if mode=='forward' and detected is not None and any(
                q['token']==token and _time(q['request_at_utc'])<_time(detected) for q in quotes):
            raise ValueError('quote_precedes_actual_detection')
        creation_row.update(detection_at_utc=detected,
            detection_latency_seconds=latency if latency is not None and latency>=0 else None,
            detection_monotonic=detection['detected_monotonic'] if detection and mode=='forward' else None,
            detection_basis='block_timestamp_to_log_response' if detected else None)
        outside = [r for r in rows if r['kind']=='buy' and r['actor'] not in members]
        sales = [r for r in rows if r['kind']=='sell' and r['actor']==subject]
        first_sale = sales[0] if sales else None
        candidates = sorted((r for r in ranges if r['eligible'] and (mode!='forward' or r['mode']=='forward')),
                            key=lambda r:(r['first'],r['last']))
        cursor, through, missing = start, None, []
        for query in candidates:
            if query['last']<cursor or query['first']>cursor:
                continue
            known = {k for k,r in events.items() if query['first']<=r['order'][0]<=query['last']
                     and r['kind'] in ('buy','sell') and r['token']==token}
            if not known.issubset(query['keys']):
                missing.append('query_omits_known_event');continue
            cursor,through = query['last']+1,query['at']
            if through>=end_at:
                break
        last = cursor-1
        if through is None or through<end_at:missing.append('complete_120_second_range_missing')
        if any(not r['header_bound'] for r in rows):missing.append('event_header_membership_missing')
        if any(a<=last and b>=start for a,b in gaps):missing.append('recorded_gap_intersects_window')
        coverage = dict(state='incomplete' if missing else 'provider_complete',missing=sorted(set(missing)),
            from_block=start,to_block=last if through is not None else None,through_timestamp=through,
            failed_transactions='unknown',other_venues='unknown',canonicality_independently_verified=False)
        launches.append(dict(token=token,creation=creation_row,events=rows,
            outside_buy_count=len(outside),outside_actors=sorted({r['actor'] for r in outside}),
            first_outside_buy=outside[0] if outside else None,
            outside_buys_before_first_creator_sale=sum(r['order']<first_sale['order'] for r in outside)
                if first_sale else None,
            first_creator_sale=first_sale,last_observed_creator_sale=sales[-1] if sales else None,
            seed_buyer_sales=[r for r in rows if r['kind']=='sell' and r['actor'] in members and r['actor']!=subject],
            coverage=coverage,quote_observations=[q for q in quotes if q['token']==token]))
    return dict(version='creator-flow-report-v1',chain='56',subject=subject,frozen_members=sorted(members),
        window_seconds=120,selection=capture['selection'],observer=capture['observer'],launches=launches,
        quotes=quotes,transaction_observations=sorted(observations.values(),key=lambda r:r['order']),
        native_net_cash=None,realized_pnl=None,ownership_proven=False,funded_authority=False,limits=list(LIMITS))
