#!/usr/bin/env python3
"""Offline target drift and own-cash/owned-lot rebalance proposals.

Concept adapted from jakeschincariol/trader-skill trader-rebalance (MIT).
Requires sibling trader-tinvest helpers and trader-budget lot allocator.
No sell proceeds are credited to the BUY budget. Never contacts a broker.
"""
from decimal import Decimal, ROUND_FLOOR, localcontext
import importlib.util
from pathlib import Path
import sys

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS / 'trader-tinvest' / 'scripts'))
from trader_common import require, dec, timestamp, cli_main

spec = importlib.util.spec_from_file_location('_trader_pack_budget', SKILLS / 'trader-budget' / 'size.py')
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)
ZERO = Decimal(0)


def _analyze(data):
    require(isinstance(data, dict), 'input must be an object')
    require(isinstance(data.get('account_id'), str) and data['account_id'], 'account_id is required')
    currency = data.get('currency')
    require(isinstance(currency, str) and len(currency) == 3, 'one currency code is required')
    timestamp(data.get('as_of'), 'as_of')
    cash = dec(data.get('own_cash'), 'own_cash', nonnegative=True)
    positions = data.get('positions')
    require(isinstance(positions, list), 'positions must be a list')
    held = {}
    for row in positions:
        require(isinstance(row, dict), 'position must be an object')
        uid = row.get('uid')
        require(isinstance(uid, str) and uid and uid not in held, 'missing or duplicate position UID')
        require(row.get('currency') == currency, 'position valuation currency must match account valuation currency')
        units = dec(row.get('units'), 'units', nonnegative=True)
        free = dec(row.get('free_units'), 'free_units', nonnegative=True)
        value = dec(row.get('value'), 'value', nonnegative=True)
        require(free <= units and (units > ZERO or value == ZERO), 'inconsistent units/free_units/value')
        held[uid] = {'units': units, 'free_units': free, 'value': value, 'source': row}
    equity = cash + sum((row['value'] for row in held.values()), ZERO)
    require(equity > ZERO, 'empty portfolio cannot be rebalanced')
    buffer, cap, minimum, blocked = budget.rules_of(data.get('rules', {}), equity)
    weights, dropped = budget.targets_of(data.get('targets'), blocked)
    band = dec(data.get('band_fraction'), 'band_fraction', nonnegative=True)
    require(band <= Decimal(1), 'band_fraction must be between 0 and 1')
    require(type(data.get('sell_untracked', False)) is bool, 'sell_untracked must be boolean')
    metadata = budget.instruments_of(data.get('instruments'), currency)
    target = budget.allocate_amounts(weights, equity - buffer, {uid: cap for uid in weights})
    rows, sells, wanted, models, notes = [], [], {}, {}, []
    for uid in sorted(set(held) | set(weights)):
        position = held.get(uid, {'units': ZERO, 'free_units': ZERO, 'value': ZERO, 'source': {}})
        current, goal = position['value'], target.get(uid, ZERO)
        drift = (current - goal) / equity
        tracked = uid in weights
        row = {'uid': uid, 'units': position['units'], 'value': current,
               'current_weight': current / equity, 'target_weight': goal / equity,
               'drift_fraction': drift, 'target_value': goal, 'action': 'HOLD'}
        rows.append(row)
        if not tracked and not data.get('sell_untracked', False):
            row['action'] = 'UNTRACKED_LEFT_ALONE'
            continue
        if tracked and abs(drift) <= band:
            continue
        require(uid in metadata, 'missing metadata for drifted instrument: ' + uid)
        if tracked:
            budget.bind_target(uid, data['targets'], metadata[uid])
        model = budget.cost_model(metadata[uid], currency)
        models[uid] = model
        if goal > current:
            wanted[uid] = min(goal - current, cap)
            row['action'] = 'BUY_CANDIDATE'
            continue
        row['action'] = 'SELL_CANDIDATE'
        source = position['source']
        sell_cap = source.get('sell_max_lots')
        if type(sell_cap) is not int or sell_cap < 0:
            notes.append({'uid': uid, 'reason': 'SELL needs confirmed sell_max_lots; no proposal emitted'})
            continue
        lot_size = metadata[uid]['lot_size']
        owned_lots = int((position['free_units'] / lot_size).to_integral_value(rounding=ROUND_FLOOR))
        value_per_lot = current * lot_size / position['units'] if position['units'] else ZERO
        require(value_per_lot > ZERO, 'cannot size sale from zero position valuation')
        desired = current - goal
        maximum = data.get('rules', {}).get('max_order')
        if maximum is not None:
            desired = min(desired, dec(maximum, 'max_order', positive=True))
        lots = min(owned_lots, sell_cap, int((desired / value_per_lot).to_integral_value(rounding=ROUND_FLOOR)))
        estimated_gross = lots * (model['principal_per_lot'] + model['nkd_per_lot'])
        estimated_fee = lots * (model['fee_per_lot'] + model['other_per_lot']) + model['fixed_fee'] if lots else ZERO
        if lots == 0 or estimated_gross < minimum or estimated_gross < estimated_fee:
            notes.append({'uid': uid, 'reason': 'sale below whole-lot/minimum/free-position or cost limit'})
            continue
        sells.append({'uid': uid, 'class_code': metadata[uid]['class_code'], 'currency': currency,
                      'side': 'SELL', 'lots': lots, 'units': str(lots * lot_size),
                      'estimated_net_proceeds': estimated_gross - estimated_fee,
                      'proceeds_in_buy_budget': False})
    spendable = max(ZERO, cash - buffer)
    buy_amounts = budget.allocate_amounts(wanted, spendable, wanted) if wanted else {}
    counts, costs = budget.lot_plan(wanted, buy_amounts, wanted, models, spendable, minimum) if wanted else ({}, {})
    buys = [{'uid': uid, 'class_code': metadata[uid]['class_code'], 'currency': currency,
             'side': 'BUY', 'lots': counts[uid], 'units': str(counts[uid] * metadata[uid]['lot_size']),
             'estimated_cash_cost': costs[uid]} for uid in sorted(wanted) if counts[uid]]
    spent = sum(costs.values(), ZERO)
    require(spent <= spendable, 'rebalance BUY proposals exceed current own cash')
    return {'status': 'ANALYZED', 'mode': 'offline', 'execution_enabled': False,
            'account_id': data['account_id'], 'currency': currency, 'as_of': data['as_of'],
            'equity': equity, 'own_cash': cash, 'cash_buffer': buffer, 'band_fraction': band,
            'drift': rows, 'proposals': sells + buys, 'buy_cost_from_current_cash': spent,
            'remaining_current_cash': cash - spent, 'future_sell_proceeds_credited': False,
            'dropped_targets': dropped, 'notes': notes,
            'next': 'Fresh exact broker estimates plus BUY/SELL guards are required; this is not an execution batch.'}


def analyze(data):
    with localcontext() as context:
        context.prec = 80
        return _analyze(data)


if __name__ == '__main__':
    raise SystemExit(cli_main(analyze, __doc__))
