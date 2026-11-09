#!/usr/bin/env python3
"""Offline lot-aware allocation of own cash. Analytical proposals, never orders.

Concept adapted from jakeschincariol/trader-skill trader-budget/size.py (MIT).
Requires installed sibling trader-tinvest for shared strict JSON/Decimal helpers.
"""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, localcontext
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'trader-tinvest' / 'scripts'))
from trader_common import require, dec, timestamp, cli_main

ZERO, ONE = Decimal(0), Decimal(1)


def targets_of(rows, blocked=()):
    require(isinstance(rows, list) and rows, 'targets must be a nonempty list')
    keep, dropped, seen = {}, [], set()
    for row in rows:
        require(isinstance(row, dict), 'target must be an object')
        uid = row.get('uid')
        require(isinstance(uid, str) and uid and uid not in seen, 'missing or duplicate target UID')
        seen.add(uid)
        weight = dec(row.get('weight'), 'target weight', nonnegative=True)
        require(weight <= ONE, 'target weights use fractions, not percentages')
        if uid in blocked or weight == ZERO:
            dropped.append({'uid': uid, 'reason': 'blocked or zero weight'})
        else:
            keep[uid] = weight
    require(keep, 'no positive unblocked targets')
    return keep, dropped


def instruments_of(rows, currency):
    require(isinstance(rows, list), 'instruments must be a list')
    out = {}
    for row in rows:
        require(isinstance(row, dict), 'instrument must be an object')
        uid = row.get('uid')
        require(isinstance(uid, str) and uid and uid not in out, 'missing or duplicate instrument UID')
        out[uid] = row
    return out


def cost_model(row, currency):
    require(row.get('currency') == currency, 'instrument currency must match the authoritative cash pool')
    require(row.get('kind') in ('share', 'bond', 'fund', 'spot_currency'), 'unsupported or unknown cash instrument kind')
    require(row.get('leveraged') is False and row.get('financing') == 'fully_paid_cash',
            'fully paid nonleveraged instrument classification is required')
    require(isinstance(row.get('class_code'), str) and row['class_code'], 'class_code is required')
    require(type(row.get('lot_size')) is int and row['lot_size'] > 0, 'lot_size must be a positive integer')
    require(isinstance(row.get('cost_source'), str) and row['cost_source'], 'cost_source is required')
    parts = {key: dec(row.get(key), key, positive=key == 'principal_per_lot',
                      nonnegative=key != 'principal_per_lot')
             for key in ('principal_per_lot', 'fee_per_lot', 'nkd_per_lot', 'other_per_lot', 'fixed_fee')}
    parts['per_lot'] = sum((parts[key] for key in
                            ('principal_per_lot', 'fee_per_lot', 'nkd_per_lot', 'other_per_lot')), ZERO)
    return parts


def bind_target(uid, targets, metadata):
    """Optional identifiers carried from copycat must agree with current metadata."""
    target = next(row for row in targets if row['uid'] == uid)
    for key in ('class_code', 'currency', 'lot_size'):
        if key in target:
            require(target[key] == metadata.get(key), 'target and metadata disagree on ' + key + ': ' + uid)


def rules_of(rules, equity):
    require(isinstance(rules, dict), 'rules must be an object')
    buffer = dec(rules.get('cash_buffer_fraction', '0'), 'cash_buffer_fraction', nonnegative=True)
    require(buffer <= ONE, 'cash_buffer_fraction must be between 0 and 1')
    cap_fraction = rules.get('max_position_fraction')
    cap = equity
    if cap_fraction is not None:
        cap = dec(cap_fraction, 'max_position_fraction', positive=True)
        require(cap <= ONE, 'max_position_fraction must not exceed 1')
        cap *= equity
    maximum = rules.get('max_order')
    if maximum is not None:
        cap = min(cap, dec(maximum, 'max_order', positive=True))
    minimum = dec(rules.get('min_order', '0'), 'min_order', nonnegative=True)
    blocked = rules.get('blocked_uids', [])
    require(isinstance(blocked, list) and all(isinstance(x, str) for x in blocked), 'blocked_uids must be strings')
    return equity * buffer, cap, minimum, blocked


def allocate_amounts(weights, pool, caps):
    """Weighted water filling; unattainable capped amounts remain cash."""
    active, out, remaining = dict(weights), {}, pool
    while active:
        total = sum(active.values(), ZERO)
        trial = {uid: remaining * weight / total for uid, weight in active.items()}
        capped = [uid for uid in active if trial[uid] > caps[uid]]
        if not capped:
            out.update(trial)
            break
        for uid in capped:
            out[uid] = caps[uid]
            remaining -= caps[uid]
            del active[uid]
    return out


def lot_plan(weights, amounts, caps, models, pool, minimum):
    """Round to whole lots, then redistribute affordable residual by deficit."""
    def cost(uid, lots):
        return models[uid]['fixed_fee'] + models[uid]['per_lot'] * lots if lots else ZERO

    def affordable(uid, money, current=0):
        fixed = models[uid]['fixed_fee'] if current == 0 else ZERO
        return max(0, int(((money - fixed) / models[uid]['per_lot']).to_integral_value(rounding=ROUND_FLOOR)))

    def first_lots(uid):
        return max(1, int(((minimum - models[uid]['fixed_fee']) / models[uid]['per_lot'])
                          .to_integral_value(rounding=ROUND_CEILING)))

    counts = {uid: affordable(uid, amounts.get(uid, ZERO)) for uid in weights}
    counts = {uid: n if cost(uid, n) >= minimum else 0 for uid, n in counts.items()}
    while True:
        spent = sum((cost(uid, n) for uid, n in counts.items()), ZERO)
        left = max(ZERO, pool - spent)
        candidates = []
        for uid, n in counts.items():
            step = 1 if n else first_lots(uid)
            extra = cost(uid, n + step) - cost(uid, n)
            if extra <= left and cost(uid, n + step) <= caps[uid]:
                candidates.append(uid)
        if not candidates:
            break
        gaps = {uid: max(ZERO, amounts.get(uid, ZERO) - cost(uid, counts[uid])) for uid in candidates}
        active = {uid: gap for uid, gap in gaps.items() if gap > ZERO}
        if not active:
            active = {uid: weights[uid] for uid in candidates}
        slices = allocate_amounts(active, left, {uid: caps[uid] - cost(uid, counts[uid]) for uid in active})
        changed = False
        for uid, money in slices.items():
            n = affordable(uid, money, counts[uid])
            if n and (counts[uid] or n >= first_lots(uid)):
                counts[uid] += n
                changed = True
        if not changed:
            uid = max(candidates, key=lambda item: (gaps[item], weights[item], item))
            counts[uid] += 1 if counts[uid] else first_lots(uid)
    return counts, {uid: cost(uid, n) for uid, n in counts.items()}


def _analyze(data):
    require(isinstance(data, dict), 'input must be an object')
    account, currency = data.get('account_id'), data.get('currency')
    require(isinstance(account, str) and account, 'account_id is required')
    require(isinstance(currency, str) and len(currency) == 3, 'one currency code is required')
    timestamp(data.get('as_of'), 'as_of')
    cash = dec(data.get('own_cash'), 'own_cash', nonnegative=True)
    buffer, cap, minimum, blocked = rules_of(data.get('rules', {}), cash)
    weights, dropped = targets_of(data.get('targets'), blocked)
    metadata = instruments_of(data.get('instruments'), currency)
    models = {}
    for uid in weights:
        require(uid in metadata, 'missing instrument metadata: ' + uid)
        bind_target(uid, data['targets'], metadata[uid])
        models[uid] = cost_model(metadata[uid], currency)
    pool = cash - buffer
    # A target unable to reach a minimum complete lot is removed explicitly.
    for uid in list(weights):
        model = models[uid]
        least = model['fixed_fee'] + model['per_lot']
        if least > min(cap, pool) or cap < minimum or pool < minimum:
            dropped.append({'uid': uid, 'reason': 'one whole lot/minimum order cannot fit cash or cap'})
            del weights[uid]
    amounts = allocate_amounts(weights, pool, {uid: cap for uid in weights}) if weights else {}
    counts, costs = lot_plan(weights, amounts, {uid: cap for uid in weights}, models, pool, minimum) if weights else ({}, {})
    proposals = []
    for uid in sorted(weights, key=lambda item: (-weights[item], item)):
        n, row = counts[uid], metadata[uid]
        if n:
            proposals.append({'uid': uid, 'class_code': row['class_code'], 'currency': currency,
                              'side': 'BUY', 'lots': n, 'units': str(n * row['lot_size']),
                              'source_weight': weights[uid], 'target_cash': amounts[uid],
                              'capped': amounts[uid] == cap, 'estimated_cash_cost': costs[uid],
                              'estimated_principal': n * models[uid]['principal_per_lot'],
                              'cost_source': row['cost_source']})
    spent = sum(costs.values(), ZERO)
    require(spent <= pool, 'allocation exceeded own cash')
    return {'status': 'ANALYZED', 'mode': 'offline', 'execution_enabled': False,
            'account_id': account, 'currency': currency, 'as_of': data['as_of'],
            'own_cash': cash, 'cash_buffer': buffer, 'estimated_spend': spent,
            'remaining_own_cash': cash - spent, 'rounding_or_cap_idle': pool - spent,
            'dropped': dropped, 'proposals': proposals,
            'next': 'Analytical quantities only. Obtain exact fresh broker estimates, lot limits and trader-risk checks.'}


def analyze(data):
    with localcontext() as context:
        context.prec = 80
        return _analyze(data)


if __name__ == '__main__':
    raise SystemExit(cli_main(analyze, __doc__))
