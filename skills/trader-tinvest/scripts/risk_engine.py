"""Combined OFFLINE proposal checks; no broker operations or permissions."""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json

from trader_common import dec, load_companion, require, timestamp


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _check_bundle(bundle, now=None):
    now = now or datetime.now(timezone.utc)
    errors, checks, orders = [], {}, []
    try:
        require(now.tzinfo is not None and now.utcoffset() is not None,
                'review time must include timezone')
        require(isinstance(bundle, dict) and bundle.get('schema_version') == 1,
                'schema_version 1 is required')
        account = bundle.get('account_id')
        require(isinstance(account, str) and account.strip(), 'account_id is required')
        require(bundle.get('execution_enabled', False) is False, 'execution must remain disabled')
        daily = bundle.get('daily_risk')
        require(isinstance(daily, dict) and daily.get('account_id') == account,
                'daily risk for the same account is required')
        # The daily baseline must match the requested review date in Moscow.
        from zoneinfo import ZoneInfo
        review_day = now.astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
        require(daily.get('baseline', {}).get('day') == review_day,
                'daily baseline does not match the Moscow review date')
        daily_snapshot = daily.get('snapshot')
        load_companion('cash_guard').fresh(daily_snapshot, now, 60, 'daily_risk.snapshot')
        daily_as_of = timestamp(daily_snapshot['as_of'])
        checks['daily'] = load_companion('daily_loss').evaluate(daily)
        if not checks['daily']['allow_new_proposals']:
            errors.append('daily loss control blocks new proposals')
        require(bundle.get('buy_plan') is not None or bundle.get('sell_plan') is not None,
                'at least one proposal plan is required')
        for key, module in (('buy_plan', 'cash_guard'), ('sell_plan', 'sell_guard')):
            plan = bundle.get(key)
            if plan is None:
                continue
            require(isinstance(plan, dict) and plan.get('account_id') == account,
                    key + ' account mismatch')
            report = load_companion(module).validate_plan(plan, now)
            checks[key] = report
            if report['status'] != 'PASS':
                errors.extend(key + ': ' + reason for reason in report['reasons'])
            require(isinstance(plan.get('orders'), list), key + ' orders missing')
            plan_as_of = timestamp(plan['snapshot']['as_of'])
            require(abs((plan_as_of - daily_as_of).total_seconds()) <= 5,
                    key + ' snapshot and daily equity observation differ by more than five seconds')
            orders.extend(plan['orders'])

        seen, sides, lots_by_uid, own_caps = set(), {}, defaultdict(int), {}
        metadata_by_uid, uid_by_broker_id = {}, {}
        for order in orders:
            require(isinstance(order, dict), 'order must be an object')
            cid = order.get('client_id')
            require(isinstance(cid, str) and cid and cid not in seen,
                    'missing or duplicate client_id across plans')
            seen.add(cid)
            instrument = order.get('instrument', {})
            require(isinstance(instrument, dict), 'instrument missing')
            uid = instrument.get('uid')
            require(isinstance(uid, str) and uid, 'instrument UID missing')
            side = order.get('side')
            require(uid not in sides or sides[uid] == side, 'opposite sides for the same UID')
            sides[uid] = side
            require(order.get('price_unit') == 'settlement_currency',
                    'normalize limit price to settlement currency; raw points are unsupported')
            currency = order.get('currency')
            require(isinstance(currency, str) and len(currency) == 3 and currency.islower(),
                    'normalized settlement currency is required')
            lot = instrument.get('lot_size')
            require(type(lot) is int and lot > 0, 'positive instrument lot_size required')
            price = dec(order.get('limit_price'), 'limit_price', positive=True)
            step = dec(instrument.get('price_step'), 'settlement-currency price_step', positive=True)
            metadata = (instrument.get('ticker'), instrument.get('class_code'), lot, step,
                        currency, instrument.get('kind'), instrument.get('financing'),
                        instrument.get('leveraged'))
            require(uid not in metadata_by_uid or metadata_by_uid[uid] == metadata,
                    'inconsistent canonical instrument metadata for UID ' + uid)
            metadata_by_uid[uid] = metadata
            ticker, board = instrument.get('ticker'), instrument.get('class_code')
            if isinstance(ticker, str) and ticker and isinstance(board, str) and board:
                broker_id = ticker + '_' + board
                require(broker_id not in uid_by_broker_id or uid_by_broker_id[broker_id] == uid,
                        'broker instrument identity maps to multiple UIDs')
                uid_by_broker_id[broker_id] = uid
            require(price % step == 0, 'limit price is not a multiple of normalized price step')
            require(order.get('quote', {}).get('instrument_uid') == uid,
                    'quote UID is not bound to the order')
            if side == 'BUY':
                estimate = order.get('estimate', {})
                require(estimate.get('currency') == currency, 'estimate currency mismatch')
                binding = estimate.get('binding', {})
                expected = {'account_id': account, 'instrument_uid': uid,
                            'lots': order.get('lots'), 'limit_price': order['limit_price'],
                            'side': 'BUY'}
                require(binding == expected, 'cost estimate is not bound to the exact BUY')
                require(dec(estimate.get('principal'), 'principal', positive=True)
                        >= price * order['lots'] * lot,
                        'principal is smaller than normalized price times units')
                cap = order.get('own_funds_limit')
                require(isinstance(cap, dict), 'bound own-funds limit missing')
                load_companion('cash_guard').fresh(cap, now, 30, 'own_funds_limit')
                require(cap.get('source') == 'buy_limits' and cap.get('verified') is True,
                        'confirmed buy_limits source required; margin limits forbidden')
                require(cap.get('account_id') == account and cap.get('instrument_uid') == uid
                        and cap.get('limit_price') == order['limit_price'],
                        'own-funds limit identity mismatch')
                n = cap.get('max_lots')
                require(type(n) is int and n >= 0 and n == order.get('own_funds_max_lots'),
                        'own-funds lot cap mismatch')
                require(type(order.get('lots')) is int and order['lots'] > 0, 'invalid lots')
                lots_by_uid[uid] += order['lots']
                own_caps[uid] = min(own_caps.get(uid, n), n)
        for uid, total in lots_by_uid.items():
            require(total <= own_caps[uid], 'batch reuses an own-funds lot cap for ' + uid)

        limits = bundle.get('limits', {})
        require(isinstance(limits, dict), 'limits must be an object')
        allowed_limits = {'blocked_uids', 'orders_already_recorded_today', 'max_orders_per_day',
                          'recorded_client_ids', 'amount_currency', 'min_order_amount',
                          'max_order_amount', 'max_position_fraction', 'position_values',
                          'equity', 'valuation_currency', 'valuation_as_of'}
        require(not (set(limits) - allowed_limits), 'unknown risk limit fields')
        blocked = limits.get('blocked_uids', [])
        require(isinstance(blocked, list), 'blocked_uids must be a list')
        require(not any(o['instrument']['uid'] in blocked for o in orders), 'blocked instrument')
        recorded = limits.get('recorded_client_ids', [])
        require(isinstance(recorded, list) and all(isinstance(x, str) for x in recorded),
                'recorded_client_ids must be a list of strings')
        require(not (seen & set(recorded)), 'proposal client_id already recorded')
        daily_count = limits.get('orders_already_recorded_today', 0)
        require(type(daily_count) is int and daily_count >= 0, 'invalid recorded order count')
        if 'max_orders_per_day' in limits:
            maximum = limits['max_orders_per_day']
            require(type(maximum) is int and maximum >= 0, 'invalid daily order limit')
            require(daily_count + len(orders) <= maximum, 'daily order count limit exceeded')
        if 'max_order_amount' in limits or 'min_order_amount' in limits:
            # These optional monetary limits apply to BUY full cost only.
            for o in orders:
                if o.get('side') != 'BUY':
                    continue
                value = dec(o['estimate']['worst_case_total'], 'order cost', positive=True)
                require(o['currency'] == limits.get('amount_currency'), 'limit currency mismatch')
                if 'max_order_amount' in limits:
                    require(value <= dec(limits['max_order_amount'], 'max_order_amount', nonnegative=True),
                            'maximum order amount exceeded')
                if 'min_order_amount' in limits:
                    require(value >= dec(limits['min_order_amount'], 'min_order_amount', nonnegative=True),
                            'minimum order amount not reached')
        if 'max_position_fraction' in limits:
            fraction = dec(limits['max_position_fraction'], 'max_position_fraction', positive=True)
            require(fraction <= 1, 'position fraction exceeds one')
            values = limits.get('position_values')
            require(isinstance(values, dict), 'authoritative position valuations required')
            require(limits.get('valuation_currency') == daily['currency'].lower(),
                    'concentration valuation must use the daily equity currency')
            require(timestamp(limits.get('valuation_as_of')) == daily_as_of,
                    'position valuations must share the daily equity observation time')
            equity = dec(daily_snapshot.get('current_equity'), 'daily current equity', positive=True)
            if 'equity' in limits:
                require(dec(limits['equity'], 'equity', positive=True) == equity,
                        'concentration equity contradicts daily current equity')
            added = defaultdict(Decimal)
            for o in orders:
                if o.get('side') == 'BUY':
                    require(o['currency'] == limits.get('valuation_currency'), 'valuation currency mismatch')
                    added[o['instrument']['uid']] += dec(o['estimate']['principal'], 'principal', positive=True)
            for uid, value in added.items():
                require(uid in values, 'missing authoritative value for ' + uid)
                require(dec(values[uid], 'position value', nonnegative=True) + value <= equity * fraction,
                        'position concentration limit exceeded for ' + uid)
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        errors.append(str(exc))
    try:
        digest = fingerprint(bundle)
    except (ValueError, TypeError):
        digest = None
        errors.append('bundle cannot be represented as finite JSON')
    return {'schema_version': 1, 'module': 'trader-risk', 'mode': 'offline',
            'status': 'BLOCK' if errors else 'PASS_OFFLINE', 'errors': errors,
            'checks': checks, 'review_as_of': now.isoformat(),
            'bundle_fingerprint': digest, 'execution_enabled': False,
            'limitations': ['normalization assertions are not broker authentication',
                            'SELL quantity validation does not establish fees or proceeds',
                            'no account reservation or execution permission']}


def check_bundle(bundle, now=None):
    with localcontext() as context:
        context.prec = 100
        return _check_bundle(bundle, now)
