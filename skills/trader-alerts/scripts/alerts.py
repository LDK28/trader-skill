#!/usr/bin/env python3
"""Evaluate supplied quotes and exchange intervals, returning events and state."""
import copy
from datetime import timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'trader-tinvest' / 'scripts'))
from trader_common import cli_main, dec, decimal_str, require, timestamp


def clock(schedule, now):
    if not schedule:
        return {'status': 'UNKNOWN', 'reason': 'No supplied exchange schedule', 'session_id': None}
    require(isinstance(schedule, dict), 'exchange schedule must be an object')
    start = timestamp(schedule.get('coverage_start'), 'schedule.coverage_start')
    end = timestamp(schedule.get('coverage_end'), 'schedule.coverage_end')
    require(start < end, 'schedule coverage interval is invalid')
    if not start <= now < end:
        return {'status': 'UNKNOWN', 'reason': 'Outside verified schedule coverage', 'session_id': None}
    intervals = schedule.get('intervals')
    require(isinstance(intervals, list), 'schedule intervals must be an array, including an explicit empty array for a closed day')
    parsed = []
    for item in intervals:
        require(isinstance(item, dict), 'session interval must be an object')
        opened, closed = timestamp(item.get('start'), 'session.start'), timestamp(item.get('end'), 'session.end')
        require(start <= opened < closed <= end, 'session lies outside schedule coverage')
        parsed.append((opened, closed, item.get('id') or opened.isoformat()))
    parsed.sort()
    require(all(a[1] <= b[0] for a, b in zip(parsed, parsed[1:])), 'overlapping exchange intervals are ambiguous')
    for opened, closed, session_id in parsed:
        if opened <= now < closed:
            return {'status': 'OPEN', 'session_id': session_id, 'session_start': opened.isoformat(), 'session_end': closed.isoformat()}
    return {'status': 'CLOSED', 'session_id': None, 'reason': 'Outside supplied sessions within verified coverage'}


def fingerprint(rule):
    return hashlib.sha256(json.dumps(rule, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def seconds(value, name, default):
    value = default if value is None else value
    require(isinstance(value, int) and not isinstance(value, bool) and value >= 0, name + ' must be a nonnegative integer')
    return value


def evaluate(payload):
    require(isinstance(payload, dict), 'input must be an object')
    now = timestamp(payload.get('as_of'), 'as_of')
    state = copy.deepcopy(payload.get('state') or {})
    require(isinstance(state, dict), 'state must be an object')
    if state.get('last_run_at'):
        require(timestamp(state['last_run_at']) <= now, 'out-of-order evaluation as_of')
    instruments, states = state.setdefault('instruments', {}), state.setdefault('rules', {})
    require(isinstance(instruments, dict) and isinstance(states, dict), 'state instruments/rules must be objects')
    schedules = payload.get('exchange_schedules', {})
    require(isinstance(schedules, dict), 'exchange_schedules must be an object')
    clocks = {exchange: clock(schedule, now) for exchange, schedule in schedules.items()}
    rules = payload.get('rules')
    require(isinstance(rules, list), 'rules must be an array')
    ids = set()
    for rule in rules:
        require(isinstance(rule, dict) and isinstance(rule.get('id'), str) and bool(rule['id']), 'alert rule id is required')
        require(rule['id'] not in ids, 'duplicate alert rule id')
        ids.add(rule['id'])
        require(rule.get('type') in ('above', 'below', 'stop', 'target', 'pct_move', 'opening_bell'), 'unsupported alert type')
        cooldown = seconds(rule.get('cooldown_seconds'), 'cooldown_seconds', 0)
        require('rearm' not in rule or isinstance(rule['rearm'], bool), 'rearm must be boolean')
        require('fire_on_initial' not in rule or isinstance(rule['fire_on_initial'], bool), 'fire_on_initial must be boolean')
        require('market_hours_only' not in rule or isinstance(rule['market_hours_only'], bool), 'market_hours_only must be boolean')
        if rule['type'] == 'opening_bell':
            require(isinstance(rule.get('exchange'), str) and bool(rule['exchange']), 'opening_bell needs an exchange')
        else:
            require(all(isinstance(rule.get(k), str) and bool(rule[k]) for k in ('uid', 'class_code', 'currency')), 'price rule requires UID, class_code and currency')
            if rule['type'] == 'pct_move':
                dec(rule.get('pct'), 'rule.pct', positive=True)
                dec(rule.get('reference_price'), 'reference_price', positive=True)
                require(rule.get('direction') in ('up', 'down', 'either'), 'pct_move direction is required')
            else:
                dec(rule.get('price'), 'rule.price', positive=True)
        digest = fingerprint(rule)
        prior = states.get(rule['id'])
        require(prior is None or isinstance(prior, dict), 'rule state must be an object')
        if prior:
            require(isinstance(prior.get('armed'), bool) and (prior.get('last_hit') is None or isinstance(prior.get('last_hit'), bool)), 'invalid armed/last_hit state')
            if prior.get('last_triggered_at'):
                require(timestamp(prior['last_triggered_at']) <= now, 'rule state contains a future trigger')
        require(prior is None or prior.get('fingerprint') == digest, 'rule configuration changed: use a new rule id or explicitly remove its prior state')
        states.setdefault(rule['id'], {'fingerprint': digest, 'armed': True, 'last_hit': None})
    max_age = seconds(payload.get('max_quote_age_seconds'), 'max_quote_age_seconds', 900)
    require(max_age > 0, 'max_quote_age_seconds must be positive')
    quotes = payload.get('quotes', [])
    require(isinstance(quotes, list), 'quotes must be an array')
    prepared = []
    for quote in quotes:
        require(isinstance(quote, dict) and all(isinstance(quote.get(k), str) and bool(quote[k]) for k in ('uid', 'class_code', 'currency')), 'quote needs UID, class_code and currency')
        when = timestamp(quote.get('as_of'), 'quote.as_of')
        price = dec(quote.get('price'), 'quote.price', positive=True)
        prepared.append((when, quote, price))
    prepared.sort(key=lambda item: (item[0], item[1]['uid'], item[1]['class_code']))
    events, skipped = [], []
    for when, quote, price in prepared:
        key = quote['uid'] + '|' + quote['class_code']
        previous = instruments.get(key)
        require(previous is None or isinstance(previous, dict), 'instrument state must be an object')
        if previous:
            require(timestamp(previous.get('as_of'), 'state quote.as_of') <= now, 'instrument state contains a future quote')
        if when > now or now - when > timedelta(seconds=max_age):
            skipped.append({'uid': quote['uid'], 'class_code': quote['class_code'], 'reason': 'future_or_stale_quote'})
            continue
        if previous and when <= timestamp(previous.get('as_of'), 'state quote.as_of'):
            if when == timestamp(previous['as_of']) and (previous.get('price') != decimal_str(price) or str(previous.get('currency', '')).lower() != quote['currency'].lower()):
                raise ValueError('conflicting quotes share an instrument and timestamp')
            skipped.append({'uid': quote['uid'], 'class_code': quote['class_code'], 'reason': 'duplicate_or_out_of_order_quote'})
            continue
        for rule in rules:
            if rule['type'] == 'opening_bell' or (rule['uid'], rule['class_code']) != (quote['uid'], quote['class_code']):
                continue
            rid, record = rule['id'], states[rule['id']]
            if rule['currency'].lower() != quote['currency'].lower():
                skipped.append({'rule_id': rid, 'reason': 'quote_currency_mismatch'})
                continue
            if rule.get('market_hours_only') is True:
                status = clocks.get(rule.get('exchange'), {'status': 'UNKNOWN'})
                if status['status'] != 'OPEN':
                    skipped.append({'rule_id': rid, 'reason': 'market_' + status['status'].lower()})
                    continue
            typ = {'stop': 'below', 'target': 'above'}.get(rule['type'], rule['type'])
            if typ == 'pct_move':
                reference = dec(rule['reference_price'], positive=True)
                threshold = dec(rule['pct'], positive=True)
                with localcontext() as ctx:
                    ctx.prec = 160
                    movement = (price - reference) * 100
                    boundary = reference * threshold
                    hit = movement >= boundary if rule['direction'] == 'up' else movement <= -boundary if rule['direction'] == 'down' else abs(movement) >= boundary
            else:
                threshold = dec(rule['price'], positive=True)
                hit = price >= threshold if typ == 'above' else price <= threshold
            was_hit = record.get('last_hit')
            crossing = was_hit is False
            initial = was_hit is None and rule.get('fire_on_initial', True)
            cooldown = seconds(rule.get('cooldown_seconds'), 'cooldown_seconds', 0)
            last_trigger = timestamp(record['last_triggered_at']) if record.get('last_triggered_at') else None
            cooldown_ready = last_trigger is None or when - last_trigger >= timedelta(seconds=cooldown)
            if hit and record.get('armed', True) and (crossing or initial) and cooldown_ready:
                events.append({'rule_id': rid, 'uid': rule['uid'], 'class_code': rule['class_code'],
                               'type': rule['type'], 'event': 'crossing' if crossing else 'condition_observed',
                               'quote_at': when.isoformat(), 'price': decimal_str(price), 'currency': quote['currency'],
                               'threshold': decimal_str(threshold), 'reference_price': rule.get('reference_price') if typ == 'pct_move' else None})
                record.update(armed=False, last_triggered_at=when.isoformat())
            elif not hit and rule.get('rearm') is True:
                record['armed'] = True
            record['last_hit'] = hit
        instruments[key] = {'as_of': when.isoformat(), 'price': decimal_str(price), 'currency': quote['currency'].lower()}
    for rule in rules:
        if rule['type'] != 'opening_bell':
            continue
        record = states[rule['id']]
        status = clocks.get(rule['exchange'], {'status': 'UNKNOWN'})
        if status['status'] == 'UNKNOWN':
            skipped.append({'rule_id': rule['id'], 'reason': 'schedule_unknown'})
        elif status['status'] == 'OPEN' and record.get('last_session_id') != status['session_id']:
            last = timestamp(record['last_triggered_at']) if record.get('last_triggered_at') else None
            if last is None or now - last >= timedelta(seconds=seconds(rule.get('cooldown_seconds'), 'cooldown_seconds', 0)):
                events.append({'rule_id': rule['id'], 'type': 'opening_bell', 'event': 'session_observed_open',
                               'exchange': rule['exchange'], 'session_id': status['session_id'], 'session_start': status['session_start'], 'observed_at': now.isoformat()})
                record.update(last_session_id=status['session_id'], last_triggered_at=now.isoformat())
    state['last_run_at'] = now.isoformat()
    return {'status': 'OK', 'mode': 'offline', 'execution_enabled': False, 'as_of': now.isoformat(),
            'as_of_msk': now.astimezone(timezone(timedelta(hours=3))).isoformat(),
            'market_status': clocks, 'events': events, 'skipped': skipped, 'state': state,
            'note': 'Events are offline observations, not messages, orders, or an installed schedule. Persist state between checks.'}


if __name__ == '__main__':
    raise SystemExit(cli_main(evaluate, __doc__))
