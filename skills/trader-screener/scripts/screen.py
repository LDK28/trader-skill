#!/usr/bin/env python3
"""Screen normalized point-in-time metrics with explicit units and availability."""
from decimal import Decimal, localcontext
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'trader-tinvest' / 'scripts'))
from trader_common import cli_main, dec, decimal_str, require, timestamp

OPS = {'>', '>=', '<', '<=', '==', '!=', 'between', 'in', 'not in'}


def metric_value(row, rule, cutoff):
    name, unit = rule.get('metric'), rule.get('unit')
    require(isinstance(name, str) and unit in ('money', 'money_per_share', 'percent', 'ratio', 'count', 'text'), 'rule metric and supported unit are required')
    metrics = row.get('metrics')
    require(isinstance(metrics, dict), 'metrics must be an object')
    raw = metrics.get(name)
    require(isinstance(raw, dict) and raw.get('value') is not None, name + ': missing metric')
    require(raw.get('unit') == unit, name + ': incompatible unit')
    if unit in ('money', 'money_per_share'):
        require(bool(rule.get('currency')) and str(raw.get('currency', '')).lower() == str(rule['currency']).lower(), name + ': incompatible currency')
    # Both observation and publication must be available at the research cutoff.
    observed = raw.get('as_of', row.get('as_of'))
    published = raw.get('published_at', row.get('published_at'))
    require(timestamp(observed, name + '.as_of') <= cutoff and timestamp(published, name + '.published_at') <= cutoff,
            name + ': future observation or publication')
    if unit == 'text':
        require(isinstance(raw['value'], str), name + ': text metric must contain a string')
        return raw['value']
    return dec(raw['value'], name)


def compare(have, rule):
    op, want = rule['op'], rule['value']
    numeric = rule['unit'] != 'text'
    convert = (lambda v: dec(v, 'rule value')) if numeric else (lambda v: v)
    if op in ('in', 'not in'):
        require(isinstance(want, list) and len(want) > 0, op + ' needs a nonempty array')
        values = [convert(v) for v in want]
        return (have in values) == (op == 'in')
    if op == 'between':
        require(numeric and isinstance(want, list) and len(want) == 2, 'between needs two numeric boundaries')
        low, high = [convert(v) for v in want]
        require(low <= high, 'between boundaries are reversed')
        return low <= have <= high
    want = convert(want)
    if not numeric:
        require(isinstance(want, str) and op in ('==', '!='), 'text supports equality or membership only')
    return {'>': lambda: have > want, '>=': lambda: have >= want,
            '<': lambda: have < want, '<=': lambda: have <= want,
            '==': lambda: have == want, '!=': lambda: have != want}[op]()


def evaluate(payload):
    require(isinstance(payload, dict), 'input must be an object')
    cutoff = timestamp(payload.get('as_of'), 'as_of')
    companies, spec = payload.get('companies'), payload.get('spec')
    require(isinstance(companies, list) and isinstance(spec, dict), 'companies array and spec object are required')
    rules, rank = spec.get('rules', []), spec.get('rank', [])
    require(isinstance(rules, list) and isinstance(rank, list) and bool(rules or rank), 'at least one rule or rank criterion is required')
    require(spec.get('missing', 'fail') == 'fail', 'missing metrics must fail; skip is unsupported')
    for rule in rules:
        require(isinstance(rule, dict) and rule.get('op') in OPS and 'value' in rule, 'invalid screen rule')
    for item in rank:
        require(isinstance(item, dict) and item.get('order', 'desc') in ('asc', 'desc') and item.get('unit') != 'text', 'invalid ranking criterion')
        dec(item.get('weight', '1'), 'rank weight', positive=True)
    basis = spec.get('basis')
    require(isinstance(basis, dict) and all(basis.get(k) for k in ('standard', 'scope', 'period_type')), 'explicit standard/scope/period_type basis is required')
    results, numeric = [], {}
    seen = set()
    for index, row in enumerate(companies):
        reasons, passed_rules = [], []
        identity = (row.get('uid'), row.get('class_code')) if isinstance(row, dict) else (None, None)
        result = {'uid': identity[0], 'class_code': identity[1], 'ticker': row.get('ticker') if isinstance(row, dict) else None,
                  'passed': False, 'reasons': reasons, 'passed_rules': passed_rules, 'score': None}
        if isinstance(row, dict):
            result.update({key: row[key] for key in ('as_of', 'published_at', 'period_start', 'period_end',
                          'standard', 'scope', 'period_type', 'source_url') if key in row})
        try:
            require(isinstance(row, dict) and all(isinstance(v, str) and bool(v) for v in identity), 'canonical uid and class_code are required')
            # Analyst emits multiple accounting bases; select one explicitly.
            require(all(row.get(k) == v for k, v in basis.items()), 'incompatible accounting or period basis')
            require(identity not in seen, 'duplicate instrument on the selected basis')
            seen.add(identity)
            require(timestamp(row.get('as_of'), 'row.as_of') <= cutoff and timestamp(row.get('published_at'), 'row.published_at') <= cutoff, 'row contains lookahead data')
            availability = row.get('availability')
            require(isinstance(availability, dict), 'availability metadata is missing')
            for flag in ('api_trade_available', 'account_available', 'cash_eligible'):
                require(availability.get(flag) is True, 'availability.' + flag + ' is not explicitly verified')
            require(availability.get('uid') == identity[0] and availability.get('class_code') == identity[1], 'availability belongs to a different instrument')
            require(timestamp(availability.get('as_of'), 'availability.as_of') <= cutoff, 'availability is from the future')
            for rule in rules:
                try:
                    have = metric_value(row, rule, cutoff)
                    require(compare(have, rule), rule['metric'] + ': threshold not met')
                    passed_rules.append(rule['metric'])
                except (ValueError, TypeError) as exc:
                    reasons.append(str(exc))
            values = []
            for item in rank:
                try:
                    values.append(metric_value(row, item, cutoff))
                except (ValueError, TypeError) as exc:
                    reasons.append(str(item.get('metric')) + ': invalid ranking metric: ' + str(exc))
            if not reasons:
                result['passed'] = True
                numeric[index] = values
        except (ValueError, TypeError) as exc:
            reasons.append(str(exc))
        results.append(result)
    winners = [index for index, result in enumerate(results) if result['passed']]
    for index in winners:
        parts, total, denominator = {}, Decimal(0), Decimal(0)
        for column, item in enumerate(rank):
            values = [numeric[winner][column] for winner in winners]
            value = numeric[index][column]
            below, equal, size = sum(v < value for v in values), sum(v == value for v in values), len(values)
            with localcontext() as ctx:
                ctx.prec = 80
                percentile = ((Decimal(below) + Decimal(equal - 1) / 2) / Decimal(size - 1)) if size > 1 else Decimal(1)
                if item.get('order', 'desc') == 'asc':
                    percentile = 1 - percentile if size > 1 else Decimal(1)
                weight = dec(item.get('weight', '1'), positive=True)
                total += percentile * weight
                denominator += weight
                parts[item['metric']] = decimal_str(percentile.quantize(Decimal('0.00000001')))
        results[index]['rank_parts'] = parts
        results[index]['score'] = decimal_str((total / denominator).quantize(Decimal('0.00000001'))) if denominator else None
    results.sort(key=lambda r: (not r['passed'], -dec(r['score']) if r['score'] is not None else Decimal(0), str(r['uid']), str(r['class_code'])))
    top = spec.get('top', len(winners))
    require(isinstance(top, int) and not isinstance(top, bool) and top >= 0, 'top must be a nonnegative integer')
    return {'status': 'OK', 'mode': 'offline', 'execution_enabled': False, 'as_of': cutoff.isoformat(),
            'results': results, 'passed_count': len(winners), 'shortlist': [r for r in results if r['passed']][:top],
            'note': 'Ranking is a filter on supplied point-in-time data; scores are not target weights or expected returns.'}


if __name__ == '__main__':
    raise SystemExit(cli_main(evaluate, __doc__))
