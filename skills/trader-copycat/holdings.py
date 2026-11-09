#!/usr/bin/env python3
"""Offline Russian public-holdings snapshot analysis, change comparison and targets.

Concept adapted from jakeschincariol/trader-skill trader-copycat (MIT).
No SEC-as-Russian data, fetching, inferred instrument mappings or broker calls.
"""
from datetime import date
from decimal import Decimal, ROUND_DOWN, localcontext
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'trader-tinvest' / 'scripts'))
from trader_common import require, dec, timestamp, cli_main

ZERO, ONE = Decimal(0), Decimal(1)


def isin_of(value):
    require(isinstance(value, str) and bool(re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}[0-9]', value)),
            'ISIN must be an explicit 12-character uppercase identifier')
    return value


def snapshot_of(doc):
    require(isinstance(doc, dict), 'holdings snapshot must be an object')
    for key in ('entity', 'quantity_basis', 'coverage_scope'):
        require(isinstance(doc.get(key), str) and doc[key], key + ' is required')
    require(doc.get('source_kind') in ('public_ru_fund_disclosure', 'public_ru_issuer_disclosure', 'synthetic_fixture'),
            'source_kind must identify a Russian public disclosure or synthetic fixture')
    require(type(doc.get('complete')) is bool, 'complete must explicitly describe coverage')
    require(isinstance(doc.get('currency'), str) and len(doc['currency']) == 3, 'snapshot currency is required')
    source = urlsplit(doc.get('source_url', ''))
    require(source.scheme in ('https', 'http') and bool(source.hostname), 'public source_url is required')
    try:
        as_of = date.fromisoformat(doc.get('as_of', ''))
    except (ValueError, TypeError):
        raise ValueError('as_of must be an ISO calendar date') from None
    published = timestamp(doc.get('published_at'), 'published_at')
    require(published.date() >= as_of, 'publication precedes holdings date')
    require(isinstance(doc.get('holdings'), list), 'holdings must be a list')
    rows = {}
    for entry in doc['holdings']:
        require(isinstance(entry, dict), 'holding must be an object')
        isin = isin_of(entry.get('isin'))
        name = entry.get('name')
        require(isinstance(name, str) and name, 'holding name is required')
        units = dec(entry.get('units'), 'holding units', nonnegative=True)
        value = dec(entry.get('value'), 'holding value', nonnegative=True)
        require(units > ZERO or value == ZERO, 'positive holding value requires positive units')
        current = rows.setdefault(isin, {'isin': isin, 'name': name, 'units': ZERO, 'value': ZERO})
        current['units'] += units
        current['value'] += value
    return {'meta': {key: doc[key] for key in ('entity', 'source_kind', 'source_url', 'as_of',
                                               'published_at', 'currency', 'complete',
                                               'quantity_basis', 'coverage_scope')},
            'date': as_of, 'holdings': rows,
            'reported_value': sum((row['value'] for row in rows.values()), ZERO)}


def mappings_of(rows):
    require(isinstance(rows, list), 'mapping must be a list')
    out = {}
    for row in rows:
        require(isinstance(row, dict), 'mapping row must be an object')
        isin = isin_of(row.get('isin'))
        require(isin not in out, 'ambiguous duplicate ISIN mapping: ' + isin)
        for key in ('uid', 'class_code', 'source'):
            require(isinstance(row.get(key), str) and row[key], 'mapping ' + key + ' is required')
        require(isinstance(row.get('currency'), str) and len(row['currency']) == 3, 'mapping currency is required')
        require(type(row.get('lot_size')) is int and row['lot_size'] > 0, 'mapping lot_size is required')
        out[isin] = row
    return out


def compare(current, prior):
    reasons = []
    if not current['meta']['complete'] or not prior['meta']['complete']:
        reasons.append('both declared coverage snapshots must be complete')
    for key in ('entity', 'currency', 'quantity_basis', 'coverage_scope'):
        if current['meta'][key] != prior['meta'][key]:
            reasons.append('incomparable ' + key)
    if current['date'] <= prior['date']:
        reasons.append('current holdings date must be later than prior date')
    if reasons:
        return {'comparable': False, 'reasons': reasons, 'changes': []}
    changes = []
    for isin in sorted(set(current['holdings']) | set(prior['holdings'])):
        old = prior['holdings'].get(isin, {'units': ZERO, 'value': ZERO})
        new = current['holdings'].get(isin, {'units': ZERO, 'value': ZERO})
        before, after = old['units'], new['units']
        if before == ZERO and after > ZERO:
            status = 'NEW'
        elif before > ZERO and after == ZERO:
            status = 'SOLD'
        elif after > before:
            status = 'ADDED'
        elif after < before:
            status = 'TRIMMED'
        else:
            status = 'UNCHANGED'
        changes.append({'isin': isin, 'status': status, 'units_before': before,
                        'units_after': after, 'units_change': after - before,
                        'value_before': old['value'], 'value_after': new['value']})
    return {'comparable': True, 'reasons': [], 'changes': changes}


def _analyze(data):
    require(isinstance(data, dict), 'input must be an object')
    current = snapshot_of(data.get('current'))
    mapping = mappings_of(data.get('mapping'))
    top = data.get('top_n')
    require(top is None or (type(top) is int and top > 0), 'top_n must be a positive integer')
    minimum = dec(data.get('min_weight_fraction', '0'), 'min_weight_fraction', nonnegative=True)
    require(minimum <= ONE, 'min_weight_fraction must not exceed 1')
    exclusions = data.get('exclude_isins', [])
    require(isinstance(exclusions, list), 'exclude_isins must be a list')
    excluded = {isin_of(value) for value in exclusions}
    reported = current['reported_value']
    candidates, skipped = [], []
    for row in sorted(current['holdings'].values(), key=lambda item: (-item['value'], item['isin'])):
        weight = row['value'] / reported if reported else ZERO
        item = dict(row, reported_weight=weight)
        reason = ('excluded by caller' if row['isin'] in excluded else
                  'zero position value' if row['value'] == ZERO else
                  'below min_weight_fraction' if weight < minimum else None)
        if reason:
            skipped.append({'isin': row['isin'], 'reason': reason})
        else:
            candidates.append(item)
    if top is not None:
        skipped.extend({'isin': row['isin'], 'reason': 'outside top_n'} for row in candidates[top:])
        candidates = candidates[:top]
    missing = [row['isin'] for row in candidates if row['isin'] not in mapping]
    seen_uids = set()
    for row in candidates:
        if row['isin'] in mapping:
            uid = mapping[row['isin']]['uid']
            require(uid not in seen_uids, 'distinct selected ISINs map to the same UID')
            seen_uids.add(uid)
    targets = []
    selected_value = sum((row['value'] for row in candidates), ZERO)
    if not missing and selected_value > ZERO:
        # Last residual avoids a rounded collection of weights summing away from 1.
        left = ONE
        for index, row in enumerate(candidates):
            mapped = mapping[row['isin']]
            weight = ((row['value'] / selected_value).quantize(Decimal('0.000000000000000001'),
                                                              rounding=ROUND_DOWN)
                      if index < len(candidates) - 1 else left)
            left -= weight
            targets.append({'uid': mapped['uid'], 'class_code': mapped['class_code'],
                            'currency': mapped['currency'], 'lot_size': mapped['lot_size'],
                            'isin': row['isin'], 'weight': weight, 'mapping_source': mapped['source']})
    comparison = compare(current, snapshot_of(data['prior'])) if data.get('prior') is not None else None
    return {'status': 'INCOMPLETE' if missing or not candidates else 'ANALYZED',
            'mode': 'offline', 'execution_enabled': False, 'source': current['meta'],
            'denominator': 'sum of values in the declared reported coverage, not necessarily fund NAV',
            'reported_positions_value': reported, 'selected_positions_value': selected_value,
            'selected': candidates, 'targets': targets, 'target_weight_decimal_places': 18,
            'missing_mapping': missing,
            'skipped': skipped, 'comparison': comparison,
            'note': 'Historical holdings and unit-count changes do not prove trades or current holdings; targets are analytical only.'}


def analyze(data):
    with localcontext() as context:
        context.prec = 80
        return _analyze(data)


if __name__ == '__main__':
    raise SystemExit(cli_main(analyze, __doc__))
