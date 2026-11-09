#!/usr/bin/env python3
"""Compute comparable reported issuer metrics from supplied Russian filings JSON."""
from datetime import date, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'trader-tinvest' / 'scripts'))
from trader_common import cli_main, dec, decimal_str, require, timestamp

MONEY = ('revenue', 'gross_profit', 'operating_income', 'net_income')
EPS = ('eps_basic', 'eps_diluted')
PERIOD_DAYS = {'quarter': (80, 100), 'half_year': (170, 190), 'nine_month': (260, 285), 'year': (350, 380)}


def computed(value, unit, published, currency=None):
    with localcontext() as ctx:
        ctx.prec = 80
        value = value.quantize(Decimal('0.00000001'))
    result = {'value': decimal_str(value), 'unit': unit, 'published_at': published, 'derived': True}
    if currency:
        result['currency'] = currency
    return result


def normalize_report(report, cutoff):
    require(isinstance(report, dict), 'report must be an object')
    require(report.get('standard') in ('IFRS', 'RSBU'), 'report standard must be IFRS or RSBU')
    require(report.get('scope') in ('consolidated', 'standalone'), 'explicit report scope is required')
    currency = report.get('currency')
    require(isinstance(currency, str) and len(currency) == 3 and currency.isalpha(), 'report currency is required')
    currency = currency.lower()
    kind = report.get('period_type')
    require(kind in PERIOD_DAYS, 'unsupported or missing period_type')
    try:
        start, end = date.fromisoformat(report['period_start']), date.fromisoformat(report['period_end'])
    except (ValueError, KeyError, TypeError):
        raise ValueError('report period dates must be YYYY-MM-DD') from None
    lo, hi = PERIOD_DAYS[kind]
    require(lo <= (end - start).days + 1 <= hi, 'period_type and dates disagree')
    published = timestamp(report.get('published_at'), 'report published_at').astimezone(timezone.utc)
    require(published.date() >= end, 'report published before its reporting period ended')
    source = report.get('source_url')
    require(isinstance(source, str) and urlsplit(source).scheme in ('https', 'http') and bool(urlsplit(source).netloc), 'filing source_url is required')
    if published > cutoff:
        return None
    metrics = report.get('metrics')
    require(isinstance(metrics, dict), 'reported metrics must be an object')
    output = {}
    for name in MONEY + EPS:
        raw = metrics.get(name)
        if raw is None:
            continue
        require(isinstance(raw, dict), 'reported metric needs value, unit and currency')
        unit = 'money_per_share' if name in EPS else 'money'
        require(raw.get('unit') == unit and str(raw.get('currency', '')).lower() == currency, 'reported metric unit/currency mismatch')
        value = dec(raw.get('value'), name)
        scale = dec(raw.get('scale', '1'), name + '.scale', positive=True)
        with localcontext() as ctx:
            ctx.prec = 80
            value *= scale
        output[name] = {'value': decimal_str(value), 'unit': unit, 'currency': currency,
                        'published_at': published.isoformat(), 'source_url': source, 'derived': False}
    flow_basis = report.get('flow_basis', 'discrete' if kind in ('quarter', 'year') else 'year_to_date')
    require(flow_basis in ('discrete', 'year_to_date'), 'flow_basis must be explicit discrete or year_to_date')
    return {'standard': report['standard'], 'scope': report['scope'], 'currency': currency,
            'period_type': kind, 'period_start': start.isoformat(), 'period_end': end.isoformat(),
            'published_at': published.isoformat(), 'source_url': source, 'flow_basis': flow_basis,
            'metrics': output, 'restated': report.get('restated') is True}


def basis(row):
    return row['standard'], row['scope'], row['currency'], row['period_type'], row['flow_basis']


def enrich(row, reports):
    row = dict(row, metrics=dict(row['metrics']), notes=[])
    metrics = row['metrics']
    revenue = dec(metrics['revenue']['value']) if 'revenue' in metrics else None
    for field, label in (('gross_profit', 'gross_margin'), ('operating_income', 'operating_margin'), ('net_income', 'net_margin')):
        if revenue is not None and revenue > 0 and field in metrics:
            metrics[label] = computed(dec(metrics[field]['value']) / revenue * 100, 'percent', row['published_at'])
    current_start, current_end = date.fromisoformat(row['period_start']), date.fromisoformat(row['period_end'])
    prior = []
    for candidate in reports:
        s, e = date.fromisoformat(candidate['period_start']), date.fromisoformat(candidate['period_end'])
        if (basis(candidate) == basis(row) and s.year == current_start.year - 1 and e.year == current_end.year - 1
                and (s.month, s.day) == (current_start.month, current_start.day)
                and e.month == current_end.month and abs(e.day - current_end.day) <= 1):
            prior.append(candidate)
    previous = max(prior, key=lambda r: r['published_at']) if prior else None
    if previous:
        for field in MONEY + EPS:
            if field not in metrics or field not in previous['metrics']:
                continue
            before = dec(previous['metrics'][field]['value'])
            if before > 0:
                value = (dec(metrics[field]['value']) / before - 1) * 100
                metric = computed(value, 'percent', max(row['published_at'], previous['published_at']))
                metric['comparison_source_url'] = previous['source_url']
                metrics[field + '_growth_yoy'] = metric
            else:
                row['notes'].append(field + ': nonpositive prior base; percentage growth not computed')
    else:
        row['notes'].append('Comparable prior-year period is unavailable')
    if 'net_income' in metrics and 'operating_income' in metrics and dec(metrics['net_income']['value']) > dec(metrics['operating_income']['value']):
        row['notes'].append('Net income exceeds operating income; read filing context before attributing a cause')
    # Only four explicitly discrete, consecutive quarters can be added into TTM.
    quarters = sorted((r for r in reports if r['standard'] == row['standard'] and r['scope'] == row['scope']
                       and r['currency'] == row['currency'] and r['period_type'] == 'quarter'
                       and r['flow_basis'] == 'discrete' and r['period_end'] <= row['period_end']), key=lambda r: r['period_end'])[-4:]
    consecutive = len(quarters) == 4 and quarters[-1]['period_end'] == row['period_end']
    if consecutive:
        consecutive = all(date.fromisoformat(b['period_start']) == date.fromisoformat(a['period_end']) + timedelta(days=1)
                          for a, b in zip(quarters, quarters[1:]))
    if consecutive:
        for field in MONEY:
            if all(field in r['metrics'] for r in quarters):
                total = sum((dec(r['metrics'][field]['value']) for r in quarters), Decimal(0))
                metrics[field + '_ttm'] = computed(total, 'money', max(r['published_at'] for r in quarters), row['currency'])
        row['notes'].append('TTM sums four supplied discrete quarters; EPS is not summed without share-basis reconciliation')
    return row


def evaluate(payload):
    require(isinstance(payload, dict), 'input must be an object')
    cutoff = timestamp(payload.get('as_of'), 'as_of').astimezone(timezone.utc)
    companies = payload.get('companies')
    require(isinstance(companies, list), 'companies must be an array')
    rows, latest, future_count = [], [], 0
    for company in companies:
        require(isinstance(company, dict) and all(isinstance(company.get(k), str) and bool(company[k]) for k in ('uid', 'class_code')), 'company needs canonical uid and class_code')
        require(isinstance(company.get('reports'), list), 'company reports must be an array')
        reports = {}
        for raw in company['reports']:
            report = normalize_report(raw, cutoff)
            if report is None:
                future_count += 1
                continue
            key = basis(report) + (report['period_start'], report['period_end'])
            old = reports.get(key)
            require(not old or old['published_at'] != report['published_at'] or old == report, 'conflicting filings at one publication timestamp')
            if old is None or report['published_at'] > old['published_at']:
                reports[key] = report
        available = list(reports.values())
        enriched = []
        for report in sorted(available, key=lambda r: (basis(r), r['period_end'])):
            row = enrich(report, available)
            row.update({k: company[k] for k in ('uid', 'class_code', 'ticker', 'name', 'availability') if k in company})
            row['as_of'] = cutoff.isoformat()
            enriched.append(row)
        rows.extend(enriched)
        by_basis = {}
        for row in enriched:
            key = basis(row)
            if key not in by_basis or row['period_end'] > by_basis[key]['period_end']:
                by_basis[key] = row
        latest.extend(by_basis.values())
    return {'status': 'OK', 'mode': 'offline', 'execution_enabled': False, 'as_of': cutoff.isoformat(),
            'companies': latest, 'reports': rows, 'excluded_future_reports': future_count,
            'note': 'Metrics are computed from supplied normalized facts; source authenticity and PDF extraction are not independently verified.'}


if __name__ == '__main__':
    raise SystemExit(cli_main(evaluate, __doc__))
