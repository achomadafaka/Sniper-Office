"""Offline Flap creator/associated-wallet observations. No inferred payouts or ownership."""
from collections import defaultdict
import json
import re

from ._abi import decode, encode
from ._abi import DecodingError
from ._abi import keccak

from ._proofs import _hex, _mined, _quantity


VERSION = 'creator-linked-capture-v1'
MAX_BYTES = 32 * 1024 * 1024
# Official BSC IPortal ABI, also bound in memecoin_bsc_fast_lane and memecoin_bsc_volume_poll.
PORTAL = '0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0'
CREATED = '0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603'
TRADES = {'0x' + keccak(text=f'{name}(uint256,address,address,uint256,uint256,uint256,uint256)').hex(): side
          for name, side in (('TokenBought', 'buy'), ('TokenSold', 'sell'))}
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
CREATED_TYPES = ['uint256', 'address', 'uint256', 'address', 'string', 'string', 'string']
TRADE_TYPES = ['uint256', 'address', 'address', 'uint256', 'uint256', 'uint256', 'uint256']
SWAP_TYPES = ['(address,address,uint256,uint256,bytes)']


def _atomic(value):
    amount = _quantity(value)
    if amount >= 2 ** 256:
        raise ValueError('quantity_out_of_range')
    return amount


def _bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', value):
        raise ValueError('malformed_hex_data')
    return bytes.fromhex(value[2:])


def _abi(types, raw):
    words = decode(types, raw)
    if encode(types, words) != raw:
        raise ValueError('noncanonical_abi')
    return words


def _cash(rows, members):
    outgoing = incoming = internal = gas = failed = count = 0
    for row in rows:
        source, target = row['from'], row['to']
        if source in members:
            count += 1
            gas += row['gas']
            failed += int(not row['success'])
        if not row['success']:
            continue  # A revert rolls back value; the sender still pays gas.
        outgoing += row['value'] if source in members else 0
        incoming += row['value'] if target in members else 0
        internal += row['value'] if source in members and target in members else 0
    return dict(signed_transaction_count=count, failed_transaction_count=failed,
        signed_native_value_out_atomic=str(outgoing), signed_native_value_in_atomic=str(incoming),
        internal_value_transfers_atomic=str(internal), boundary_native_value_out_atomic=str(outgoing - internal),
        boundary_native_value_in_atomic=str(incoming - internal), gas_paid_atomic=str(gas),
        native_net_cash=None, realized_pnl=None,
        missing=['missing_native_payout_traces', 'complete_transaction_history_unproven',
                 'opening_and_closing_inventory_unknown'])


def analyze_capture(capture: dict) -> dict:
    """Validate supplied identities and report only observed facts from this finite sample."""
    try:
        return _analyze(capture)
    except (KeyError, TypeError, AttributeError, IndexError, DecodingError, OverflowError) as exc:
        raise ValueError('malformed_capture: ' + str(exc)) from None


def _analyze(capture):
    if not isinstance(capture, dict) or capture.get('version') != VERSION or capture.get('chain') != '56':
        raise ValueError('unsupported_capture_or_chain')
    if len(json.dumps(capture, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError('capture_too_large')
    subject = _hex(capture['subject'], 20)
    raw_members = capture['scenario_members']
    if not isinstance(raw_members, list) or not 1 <= len(raw_members) <= 64:
        raise ValueError('invalid_scenario_members')
    members = {_hex(value, 20) for value in raw_members}
    if subject not in members or len(members) != len(raw_members) or int(subject, 16) == 0:
        raise ValueError('invalid_scenario_members')
    if not isinstance(capture['coverage'], dict):
        raise ValueError('invalid_coverage')
    sections = {name: capture[name] for name in ('transactions', 'funding_transactions')}
    if any(not isinstance(rows, list) for rows in sections.values()) or sum(map(len, sections.values())) > 2000:
        raise ValueError('invalid_transaction_count')
    seen, blocks, orders, block_logs = {}, {}, {}, set()
    rows = {name: [] for name in sections}
    creators, trades, funding = [], [], []
    deltas = defaultdict(int)
    duplicate_count = total_logs = 0
    for section, bundles in sections.items():
        for bundle in bundles:
            tx, receipt, header = (bundle[k] for k in ('transaction', 'receipt', 'header'))
            if _atomic(tx['chainId']) != 56:
                raise ValueError('wrong_transaction_chain')
            sender, target, signature, order, timestamp = _mined(receipt, tx, header, '56', allow_failed=True)
            bound = json.dumps([tx, receipt, header], sort_keys=True, allow_nan=False)
            if signature in seen:
                previous_section, previous = seen[signature]
                if previous != bound:
                    raise ValueError('conflicting_transaction_duplicate')
                if previous_section != section:
                    raise ValueError('overlapping_transaction_categories')
                duplicate_count += 1
                continue
            seen[signature] = section, bound
            block_identity = (_hex(header['hash'], 32), timestamp)
            if order[0] in blocks and blocks[order[0]] != block_identity:
                raise ValueError('conflicting_block_identity')
            if tuple(order) in orders and orders[tuple(order)] != signature:
                raise ValueError('conflicting_transaction_order')
            blocks[order[0]], orders[tuple(order)] = block_identity, signature
            value = _atomic(tx['value'])
            gas = _atomic(receipt['gasUsed']) * _atomic(receipt['effectiveGasPrice'])
            success = _quantity(receipt['status']) == 1
            data = _bytes(tx['input'])
            if not success and receipt['logs']:
                raise ValueError('reverted_transaction_has_logs')
            swap = None
            if target == PORTAL and data[:4].hex() == 'ef7ec2e7':
                swap = _abi(SWAP_TYPES, data[4:])[0]
            row = dict(tx_hash=signature, order=order, timestamp=timestamp, value=value,
                       gas=gas, success=success, **{'from': sender, 'to': target})
            rows[section].append(row)
            indices = set()
            for event in receipt['logs']:
                total_logs += 1
                if total_logs > 50000:
                    raise ValueError('too_many_logs')
                index = _atomic(event['logIndex'])
                if index in indices:
                    raise ValueError('duplicate_log_index')
                indices.add(index)
                if (order[0], index) in block_logs:
                    raise ValueError('duplicate_block_log_index')
                block_logs.add((order[0], index))
                if event.get('removed', False) is not False:
                    raise ValueError('removed_log')
                topics = [_hex(t, 32) for t in event['topics']]
                raw = _bytes(event['data'])
                address = _hex(event['address'], 20)
                if section == 'funding_transactions':
                    continue
                common = dict(tx_hash=signature, order=[*order, index], timestamp=timestamp)
                if address == PORTAL and topics and topics[0] in {CREATED, *TRADES}:
                    if len(topics) != 1:
                        raise ValueError('malformed_portal_topics')
                    if topics[0] == CREATED:
                        words = _abi(CREATED_TYPES, raw)
                        if words[0] != timestamp or words[1].lower() != sender:
                            raise ValueError('unsupported_creator_actor_or_timestamp')
                        creators.append(dict(common, creator=words[1].lower(), token=words[3].lower(),
                                             event_timestamp=words[0], nonce=str(words[2])))
                    else:
                        words = _abi(TRADE_TYPES, raw)
                        if words[0] != timestamp or words[2].lower() != sender:
                            raise ValueError('unsupported_trade_actor_or_timestamp')
                        side = TRADES[topics[0]]
                        if swap and swap[1 if side == 'buy' else 0].lower() != words[1].lower():
                            raise ValueError('swap_event_token_mismatch')
                        trades.append(dict(common, side=TRADES[topics[0]], token=words[1].lower(),
                            address=words[2].lower(), event_timestamp=words[0], token_amount_atomic=str(words[3]),
                            quote_amount_raw=str(words[4]), fee_amount_raw=str(words[5]), post_price_raw=str(words[6]),
                            quote_asset=None, quote_units='unknown_quote_asset_atomic',
                            signed_native_value_atomic=str(value),
                            min_output_amount_atomic=str(swap[3]) if swap else None))
                if topics and topics[0] == TRANSFER:
                    amount = int.from_bytes(raw, 'big')
                    for actor, sign in (('0x' + topics[1][-40:], -1), ('0x' + topics[2][-40:], 1)):
                        if actor in members:
                            deltas[(address, actor)] += sign * amount
            if section == 'funding_transactions':
                funding.append(dict(tx_hash=signature, order=order, timestamp=timestamp,
                    funder=sender, recipient=target, amount_atomic=str(value if success else 0), asset='native',
                    symbol='BNB', verified=False, ownership_proven=False, recipient_credit_proven=False,
                    status='signed_native_transfer_association' if success and not data
                           else 'recipient_credit_unproven_or_unsupported'))
    creators.sort(key=lambda r: r['order'])
    trades.sort(key=lambda r: r['order'])
    launches = {r['token']: r for r in creators if r['creator'] == subject}
    buyers = defaultdict(dict)
    for trade in trades:
        creation = launches.get(trade['token'])
        if (creation and trade['side'] == 'buy' and trade['order'] > creation['order']
                and trade['address'] != subject):
            buyers[trade['address']].setdefault(trade['token'], trade)
    recurring = [dict(address=address, distinct_launches=len(found), recurring=len(found) > 1,
        tokens=sorted(found), observations=[dict(token=r['token'], order=r['order'], tx_hash=r['tx_hash'])
            for r in sorted(found.values(), key=lambda r: r['order'])], ownership_proven=False)
        for address, found in sorted(buyers.items())]
    accounting = {section: dict(subject=_cash(part, {subject}), scenario_group=_cash(part, members))
                  for section, part in rows.items()}
    return dict(version='creator-linked-report-v1', chain='56', subject=subject, scenario_members=sorted(members),
        scope='provided_transactions_only', identity_validation='supplied_receipt_transaction_header_consistency',
        canonicality_independently_verified=False, coverage=capture['coverage'],
        transaction_count=len(rows['transactions']), funding_transaction_count=len(rows['funding_transactions']),
        duplicate_transactions=duplicate_count, creator_events=creators, ordered_trades=trades,
        transaction_observations=[dict(tx_hash=r['tx_hash'], order=r['order'], timestamp=r['timestamp'],
            success=r['success'], signed_native_value_atomic=str(r['value']),
            applied_native_value_atomic=str(r['value'] if r['success'] else 0), gas_paid_atomic=str(r['gas']),
            **{'from': r['from'], 'to': r['to']}) for r in sorted(rows['transactions'], key=lambda r: r['order'])],
        recurring_launch_buyers=recurring, funding_associations=sorted(funding, key=lambda r: r['order']),
        native_cash_accounting=accounting['transactions'], financing_native_cash_accounting=accounting['funding_transactions'],
        token_transfer_deltas=[dict(token=token, address=address, delta_atomic=str(delta))
            for (token, address), delta in sorted(deltas.items()) if delta],
        inventory_status='opening_and_closing_balances_unknown', native_net_cash=None, realized_pnl=None,
        delayed_entry=dict(status='non_identifiable', net_cash=None,
            missing=['actual_detection_clock', 'actual_landing_clock', 'ordered_amount_bound_quotes',
                     'prior_cost_and_latency_calibration'],
            reason='block_order_and_source_fills_do_not_establish_our_executable_entry_or_exit'),
        funded_authority=False, ownership_proven=False)
